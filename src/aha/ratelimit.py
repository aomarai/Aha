"""Per-user sliding-window rate limiting shared by both invocation surfaces."""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Hashable

__all__ = ["RateLimiter"]


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
