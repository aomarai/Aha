"""Per-user sliding-window rate limiting shared by both invocation surfaces.

The bot depends on :class:`RateLimitBackend`, not on a concrete limiter, so where the budget is
kept can change without touching the command checks. Today the only backend is
:class:`LocalBackend`, which is per process; ADR 0001 defers a shared (Redis) backend until a
measured need (#102), at which point it implements the same protocol.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Hashable
from typing import Protocol

__all__ = ["LocalBackend", "RateLimitBackend", "RateLimiter"]


class RateLimitBackend(Protocol):
    """Where the bot-wide per-user budget lives.

    ``consume`` is a coroutine because a shared backend has to talk to a network service. It
    returns ``None`` when the call is allowed, otherwise the seconds until it would be. A shared
    backend should *fail open* (allow the call and log) when its store is unreachable, so an
    outage of the limiter does not take the bot down with it.
    """

    @property
    def rate(self) -> int:
        """Invocations allowed per window."""

    @property
    def per(self) -> float:
        """Window length in seconds."""

    async def consume(self, key: Hashable) -> float | None:
        """Record one invocation for ``key`` and say how long to wait, if it is over budget."""


class RateLimiter:
    """Sliding-window limiter keyed by an arbitrary identifier (normally a user id).

    At most ``rate`` invocations are allowed in **any** ``per``-second window.
    Unlike a fixed-window bucket, exhausting the budget just before a window
    boundary does not restore it in one jump.

    State is per process: running multiple shards or replicas means each process
    enforces its own budget (see #2 for shared state). Keys with no event inside
    the window are pruned, so memory tracks recently active users rather than
    every user the bot has ever seen.
    """

    def __init__(
        self, rate: int, per: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if rate < 1:
            raise ValueError(f"rate must be at least 1, got {rate}")
        if per <= 0:
            raise ValueError(f"per must be positive, got {per}")
        self.rate = rate
        self.per = per
        self._clock = clock
        self._events: dict[Hashable, deque[float]] = {}
        self._last_prune = -math.inf

    def consume(self, key: Hashable) -> float | None:
        """Record one invocation for ``key``.

        Returns:
            ``None`` when the call is allowed, otherwise the seconds to wait
            before the next call is allowed.
        """
        now = self._clock()
        self._prune(now)
        events = self._events.get(key)
        if events is None:
            events = self._events[key] = deque()
        while events and now - events[0] >= self.per:
            events.popleft()
        if len(events) >= self.rate:
            return events[0] + self.per - now
        events.append(now)
        return None

    def _prune(self, now: float) -> None:
        """Drop keys with no event inside the current window."""
        if now - self._last_prune < self.per:
            return
        self._last_prune = now
        for key in [key for key, events in self._events.items() if now - events[-1] >= self.per]:
            del self._events[key]


class LocalBackend:
    """The in-memory backend: a :class:`RateLimiter` presented through :class:`RateLimitBackend`.

    The limiter's ``consume`` has no ``await`` in its critical section, so awaiting this wrapper
    cannot interleave two callers: the check-and-record stays atomic on the event loop. The budget
    is per process, so a user whose interactions reach several processes can use up to
    ``rate`` times the number of processes in one window.
    """

    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

    @property
    def rate(self) -> int:
        """Invocations allowed per window."""
        return self._limiter.rate

    @property
    def per(self) -> float:
        """Window length in seconds."""
        return self._limiter.per

    async def consume(self, key: Hashable) -> float | None:
        """Record one invocation for ``key`` (see :meth:`RateLimiter.consume`)."""
        return self._limiter.consume(key)
