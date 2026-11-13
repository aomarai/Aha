"""Supervised background tasks.

A bare ``asyncio.create_task`` has two failure modes: the event loop keeps only a weak
reference, so an unreferenced task can be garbage collected mid-flight, and an exception
inside it surfaces only through the loop's default handler (or never), with no name and no
context. :class:`TaskRegistry` keeps strong references, logs every failure with the task's
name, optionally restarts the task with capped exponential back-off, tracks which critical
tasks have died for the readiness probe, and cancels everything at shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

__all__ = ["TaskRegistry"]

log = logging.getLogger(__name__)

#: First restart delay in seconds; doubles on each consecutive failure.
INITIAL_BACKOFF = 1.0

#: Upper bound for the restart delay, and the run length that counts as "healthy again".
MAX_BACKOFF = 60.0

#: Seconds :meth:`TaskRegistry.close` waits for cancelled tasks to unwind.
CLOSE_TIMEOUT = 10.0


class TaskRegistry:
    """Owns the long-running background tasks of one bot process.

    Tasks are spawned from a zero-argument *factory* returning a coroutine, not from a
    coroutine object, because a coroutine can run only once and a restart needs a fresh one.

    Args:
        sleep: Awaitable sleep used for restart back-off (injectable for tests).
        clock: Monotonic clock used to decide whether a task ran long enough to reset its
            back-off (injectable for tests).
        initial_backoff: First restart delay in seconds.
        max_backoff: Cap on the restart delay, and the run length after which the delay
            resets to ``initial_backoff``.
    """

    def __init__(
        self,
        *,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        initial_backoff: float = INITIAL_BACKOFF,
        max_backoff: float = MAX_BACKOFF,
    ) -> None:
        if (
            not math.isfinite(initial_backoff)
            or not math.isfinite(max_backoff)
            or initial_backoff <= 0
            or max_backoff < initial_backoff
        ):
            raise ValueError("backoff values must be finite, positive, and max >= initial")
        self._sleep = sleep
        self._clock = clock
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._failed: set[str] = set()
        self._closed = False
        #: Tasks currently inside :meth:`close`; they are never cancelled by a sibling closer.
        self._closers: set[object] = set()

    def __contains__(self, name: object) -> bool:
        """Return True when a task called ``name`` is still running."""
        return name in self._tasks

    def __len__(self) -> int:
        return len(self._tasks)

    def get(self, name: str) -> asyncio.Task[None] | None:
        """Return the running task called ``name``, or None."""
        return self._tasks.get(name)

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has been called; no further tasks can be spawned."""
        return self._closed

    @property
    def failed(self) -> frozenset[str]:
        """Names of critical tasks that crashed and will not be restarted."""
        return frozenset(self._failed)

    @property
    def healthy(self) -> bool:
        """True while no critical task has died for good (feeds the readiness probe)."""
        return not self._failed

    def spawn(
        self,
        factory: Callable[[], Awaitable[None]],
        *,
        name: str,
        restart: bool = False,
        critical: bool = False,
    ) -> asyncio.Task[None]:
        """Start ``factory()`` as a supervised task called ``name``.

        Args:
            factory: Zero-argument callable returning the coroutine to run.
            name: Unique, human-readable name used in logs and as the asyncio task name.
            restart: Restart the task with capped exponential back-off when it raises.
                Without it a crash is logged once and the task stays dead.
            critical: When a task that is *not* restarted crashes, mark the registry
                unhealthy so readiness can report it.

        Raises:
            RuntimeError: if the registry is closed.
            ValueError: if a live task already uses ``name``.
        """
        if self._closed:
            raise RuntimeError(f"cannot spawn {name!r}: the task registry is closed")
        if name in self._tasks:
            raise ValueError(f"a task named {name!r} is already running")
        # A replacement for a task that died for good makes the registry healthy again.
        self._failed.discard(name)
        task = asyncio.create_task(self._supervise(factory, name, restart, critical), name=name)
        self._tasks[name] = task
        task.add_done_callback(lambda _done: self._forget(name))
        return task

    def _forget(self, name: str) -> None:
        # ``spawn`` refuses a live duplicate name, so this entry is always the finished task.
        del self._tasks[name]

    async def _supervise(
        self,
        factory: Callable[[], Awaitable[None]],
        name: str,
        restart: bool,
        critical: bool,
    ) -> None:
        delay = self._initial_backoff
        while True:
            started = self._clock()
            try:
                await factory()
            except asyncio.CancelledError:
                if critical and not self._closed:
                    # Someone cancelled a critical task while the bot is running: it is gone.
                    log.error("Critical background task %s was cancelled", name)
                    self._failed.add(name)
                raise
            except Exception:
                log.exception("Background task %s failed", name)
                if self._closed:
                    return  # shutting down: never start another run
                if not restart:
                    if critical:
                        self._failed.add(name)
                    return
                if self._clock() - started >= self._max_backoff:
                    delay = self._initial_backoff
                log.warning("Restarting background task %s in %.1f s", name, delay)
                await self._sleep(delay)
                delay = min(delay * 2, self._max_backoff)
            else:
                return

    async def close(self, timeout: float = CLOSE_TIMEOUT) -> None:
        """Cancel every task and wait up to ``timeout`` seconds for them to unwind.

        Tasks that are closing the registry are never cancelled, so a task may close the
        registry it belongs to (the gateway watchdog closes the bot, which closes this
        registry) and two tasks may close it concurrently. Further
        :meth:`spawn` calls are refused. Safe to call more than once.
        """
        self._closed = True
        current = asyncio.current_task()
        self._closers.add(current)
        try:
            tasks = [task for task in self._tasks.values() if task not in self._closers]
            for task in tasks:
                task.cancel()
            if not tasks:
                return
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                log.warning(
                    "Background task %s did not stop within %.1f s", task.get_name(), timeout
                )
        finally:
            self._closers.discard(current)
