"""A durable scheduler: due jobs live in PostgreSQL and any replica may run them.

Why it is shaped this way (ADR 0001, section 5):

* **Any pod can run any job.** Delivery uses REST, which needs no gateway shard, so timed work
  scales with the replicas and survives restarts. Pods claim due rows with ``FOR UPDATE SKIP
  LOCKED`` (:class:`aha.jobstore.JobStore`), so two pods never run the same row at once.
* **At-least-once.** A pod that crashes after Discord accepted a message but before the row was
  deleted leaves a lease that expires; another pod then runs the job again. Handlers must therefore
  be idempotent where Discord allows (a deterministic message ``nonce``), and accept a rare
  duplicate where it does not.
* **Poison jobs end.** ``attempts`` is counted at claim time, so a job that keeps crashing the
  process is marked dead after ``max_attempts`` rather than looping forever; failures retry with
  exponential back-off.
* **Observable.** :attr:`lag` (seconds the oldest due job has waited) feeds the
  ``aha_scheduler_lag_seconds`` metric and its alert.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from aha.jobstore import Job

__all__ = ["DEAD_RETENTION", "Handler", "JobSource", "Scheduler"]

log = logging.getLogger(__name__)

#: How long a dead job is kept for debugging before it is deleted (seconds): seven days.
DEAD_RETENTION = 7 * 24 * 3600.0

#: Longest delay before a failed job is retried.
MAX_BACKOFF = 3600.0

#: Longest error text stored on a failed job.
_ERROR_LIMIT = 500

Handler = Callable[[Job], Awaitable[None]]


class JobSource(Protocol):
    """What the scheduler needs from the store (so tests can fake it)."""

    async def claim(self, limit: int, lease_seconds: float) -> list[Job]:
        """Claim due jobs."""

    async def complete(self, job_id: int) -> None:
        """Delete a finished job."""

    async def fail(self, job_id: int, error: str, *, retry_in: float, dead: bool) -> None:
        """Record a failure."""

    async def lag(self) -> float:
        """Seconds the oldest due job has waited."""

    async def purge_dead(self, older_than_seconds: float) -> int:
        """Delete long-dead jobs."""


class Scheduler:
    """Polls the store, runs handlers by job ``kind``, and retries or buries failures."""

    def __init__(
        self,
        store: JobSource,
        *,
        poll_interval: float = 5.0,
        batch: int = 10,
        lease: float = 60.0,
        handler_timeout: float = 30.0,
        max_attempts: int = 5,
        backoff: float = 30.0,
        on_lag: Callable[[float], None] | None = None,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if lease <= handler_timeout:
            raise ValueError("lease must be longer than handler_timeout, or jobs would run twice")
        self._store = store
        self._poll_interval = poll_interval
        self._batch = batch
        self._lease = lease
        self._handler_timeout = handler_timeout
        self._max_attempts = max_attempts
        self._backoff = backoff
        self._on_lag = on_lag
        self._sleep = sleep
        self._handlers: dict[str, Handler] = {}

    def register(self, kind: str, handler: Handler) -> None:
        """Run ``handler`` for jobs of ``kind``. One handler per kind."""
        if kind in self._handlers:
            raise ValueError(f"a handler for {kind!r} is already registered")
        self._handlers[kind] = handler

    async def run(self) -> None:
        """Poll forever. Suitable as a :class:`aha.tasks.TaskRegistry` factory."""
        while True:
            await self.run_once()
            await self._sleep(self._poll_interval)

    async def run_once(self) -> int:
        """Claim and run one batch of due jobs; return how many were claimed."""
        jobs = await self._store.claim(self._batch, self._lease)
        for job in jobs:
            await self._execute(job)
        if self._on_lag is not None:
            self._on_lag(await self._store.lag())
        purged = await self._store.purge_dead(DEAD_RETENTION)
        if purged:
            log.info("Deleted %d long-dead scheduled jobs", purged)
        return len(jobs)

    def retry_delay(self, attempts: int) -> float:
        """Seconds to wait before retrying a job that has been claimed ``attempts`` times."""
        return float(min(self._backoff * 2 ** (attempts - 1), MAX_BACKOFF))

    async def _execute(self, job: Job) -> None:
        handler = self._handlers.get(job.kind)
        try:
            if handler is None:
                # Another revision may know this kind (a rolling deploy): retry rather than lose it.
                raise LookupError(f"no handler registered for {job.kind!r}")
            async with asyncio.timeout(self._handler_timeout):
                await handler(job)
        except Exception as exc:
            dead = job.attempts >= self._max_attempts
            log.exception(
                "Scheduled job %s (%s) failed on attempt %d%s",
                job.id,
                job.kind,
                job.attempts,
                "; giving up" if dead else "",
            )
            await self._store.fail(
                job.id,
                f"{type(exc).__name__}: {exc}"[:_ERROR_LIMIT],
                retry_in=self.retry_delay(job.attempts),
                dead=dead,
            )
        else:
            await self._store.complete(job.id)
