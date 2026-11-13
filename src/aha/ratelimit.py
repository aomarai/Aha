"""Per-user command rate limiting shared by both invocation surfaces."""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable

from discord.ext import commands

__all__ = ["RateLimiter"]


class RateLimiter:
    """Token-bucket limiter keyed by an arbitrary identifier (normally a user id).

    One small bucket is kept per key for the lifetime of the process; entries are
    not evicted. That is acceptable here because the key space is bounded by the
    bot's active users, not by request volume.
    """

    def __init__(
        self, rate: int, per: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.rate = rate
        self.per = per
        self._clock = clock
        self._buckets: dict[Hashable, commands.Cooldown] = {}

    def consume(self, key: Hashable) -> float | None:
        """Consume one token for ``key``.

        Returns:
            ``None`` when the call is allowed, otherwise the seconds to wait
            before the next call is allowed.
        """
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = commands.Cooldown(self.rate, self.per)
        return bucket.update_rate_limit(now)
