"""The gateway session-start budget and the guard that protects it (#94).

Discord limits how often a bot may open gateway sessions (``session_start_limit``: ``total`` per
24 hours, ``remaining``, ``reset_after``) and a crash loop spends it with every restart. When it is
nearly gone, identifying again only locks the bot out, so the guard waits for the reset instead.
Kubernetes' own restart back-off is not enough: it retries within minutes, not hours.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["DEFAULT_FLOOR", "RESET_MARGIN", "SessionBudget", "await_budget"]

log = logging.getLogger(__name__)

#: Fraction of ``total`` below which the bot refuses to identify (10 %).
DEFAULT_FLOOR = 0.1

#: Seconds added to ``reset_after`` before looking again, so the reset has really happened.
RESET_MARGIN = 5.0


@dataclass(frozen=True, slots=True)
class SessionBudget:
    """Discord's ``session_start_limit`` for this token."""

    total: int
    remaining: int
    #: Seconds until ``remaining`` is refilled to ``total``.
    reset_after: float
    #: Identifies allowed per 5 seconds (buckets are ``shard_id % max_concurrency``).
    max_concurrency: int

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> SessionBudget:
        """Build from the ``/gateway/bot`` payload (``reset_after`` there is milliseconds)."""
        return cls(
            total=int(data["total"]),
            remaining=int(data["remaining"]),
            reset_after=int(data["reset_after"]) / 1000,
            max_concurrency=max(1, int(data.get("max_concurrency", 1))),
        )

    def is_low(self, floor: float) -> bool:
        """True when fewer than ``floor`` of the daily sessions remain."""
        return self.remaining < self.total * floor


async def await_budget(
    fetch: Callable[[], Awaitable[SessionBudget]],
    *,
    floor: float = DEFAULT_FLOOR,
    on_budget: Callable[[SessionBudget], None] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SessionBudget:
    """Return the budget once it is above ``floor``, waiting for Discord's reset if it is not.

    Every reading is logged and passed to ``on_budget`` (the metrics). A floor of ``0`` never waits.
    """
    while True:
        budget = await fetch()
        log.info(
            "Gateway session budget: %s of %s remaining, resets in %.0f s, max concurrency %s",
            budget.remaining,
            budget.total,
            budget.reset_after,
            budget.max_concurrency,
        )
        if on_budget is not None:
            on_budget(budget)
        if not budget.is_low(floor):
            return budget
        log.critical(
            "Only %s of %s gateway sessions remain (floor %.1f%%): not identifying; waiting %.0f s "
            "for the daily reset",
            budget.remaining,
            budget.total,
            floor * 100,
            budget.reset_after,
        )
        await sleep(budget.reset_after + RESET_MARGIN)
