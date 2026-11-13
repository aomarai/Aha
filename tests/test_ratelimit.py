"""Tests for the per-user sliding-window limiter."""

from __future__ import annotations

import asyncio
from collections.abc import Hashable

import pytest

from aha.ratelimit import LocalBackend, RateLimitBackend, RateLimiter
from tests.helpers import FakeClock


def test_zero_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="rate must be at least 1, got 0"):
        RateLimiter(0, 10.0)


def test_negative_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="rate must be at least 1, got -1"):
        RateLimiter(-1, 10.0)


def test_non_positive_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="per must be positive, got 0"):
        RateLimiter(1, 0.0)


def test_negative_window_is_rejected() -> None:
    with pytest.raises(ValueError, match=r"per must be positive, got -5\.0"):
        RateLimiter(1, -5.0)


def test_boundary_valid_limits_are_accepted() -> None:
    limiter = RateLimiter(1, 0.001, clock=FakeClock())

    assert (limiter.rate, limiter.per) == (1, 0.001)


def test_allows_up_to_the_rate_then_reports_the_wait() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, 10.0, clock=clock)

    assert limiter.consume(1) is None
    assert limiter.consume(1) is None
    assert limiter.consume(1) == pytest.approx(10.0)


def test_tokens_refill_after_the_window() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, 10.0, clock=clock)
    assert limiter.consume(1) is None
    assert limiter.consume(1) is not None

    clock.now += 11.0

    assert limiter.consume(1) is None


def test_window_is_sliding_not_fixed() -> None:
    clock = FakeClock(0.0)
    limiter = RateLimiter(2, 10.0, clock=clock)
    assert limiter.consume(1) is None  # t=0
    clock.now = 9.0
    assert limiter.consume(1) is None  # t=9, budget full
    clock.now = 10.5
    # t=0 fell out of the window; the t=9 event still counts.
    assert limiter.consume(1) is None
    assert limiter.consume(1) == pytest.approx(8.5)


def test_events_expire_at_the_window_boundary() -> None:
    clock = FakeClock(0.0)
    limiter = RateLimiter(1, 10.0, clock=clock)
    assert limiter.consume(1) is None

    clock.now = 10.0

    assert limiter.consume(1) is None


def test_stale_events_are_dropped_at_the_boundary() -> None:
    clock = FakeClock(0.0)
    limiter = RateLimiter(3, 10.0, clock=clock)
    assert limiter.consume(1) is None  # t=0
    clock.now = 9.0
    assert limiter.consume(1) is None  # t=9
    clock.now = 10.0
    assert limiter.consume(1) is None  # t=0 expires here
    # A second call at the same instant must not see the expired t=0 event.
    assert limiter.consume(1) is None


def test_prune_runs_at_the_exact_boundary() -> None:
    clock = FakeClock(0.0)
    limiter = RateLimiter(1, 10.0, clock=clock)
    assert limiter.consume(1) is None

    clock.now = 10.0
    assert limiter.consume(2) is None

    assert list(limiter._events) == [2]


def test_buckets_are_per_key() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, 10.0, clock=clock)

    assert limiter.consume(1) is None
    assert limiter.consume(2) is None
    assert limiter.consume(1) is not None


def test_idle_keys_are_pruned() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, 10.0, clock=clock)
    assert limiter.consume(1) is None

    clock.now += 11.0
    assert limiter.consume(2) is None

    assert list(limiter._events) == [2]


def test_active_keys_survive_pruning() -> None:
    clock = FakeClock()
    limiter = RateLimiter(3, 10.0, clock=clock)
    assert limiter.consume(1) is None  # t=100
    clock.now = 105.0
    assert limiter.consume(1) is None  # t=105, still inside the window at t=110

    clock.now = 110.0
    assert limiter.consume(2) is None

    assert set(limiter._events) == {1, 2}


def _backend(rate: int = 2, per: float = 10.0, clock: FakeClock | None = None) -> LocalBackend:
    return LocalBackend(RateLimiter(rate, per, clock=clock or FakeClock()))


def test_the_local_backend_exposes_the_limiters_rate_and_window() -> None:
    backend: RateLimitBackend = _backend(rate=3, per=7.5)

    assert (backend.rate, backend.per) == (3, 7.5)


def test_the_local_backend_allows_up_to_the_rate_then_reports_the_wait() -> None:
    clock = FakeClock(100.0)
    backend = _backend(rate=2, per=10.0, clock=clock)

    async def scenario() -> list[float | None]:
        first = await backend.consume("user")
        clock.now += 4.0
        second = await backend.consume("user")
        third = await backend.consume("user")
        clock.now += 6.0  # the first call leaves the window
        fourth = await backend.consume("user")
        return [first, second, third, fourth]

    assert asyncio.run(scenario()) == [None, None, pytest.approx(6.0), None]


def test_the_local_backend_keeps_a_separate_budget_per_key() -> None:
    backend = _backend(rate=1)

    async def scenario() -> list[float | None]:
        return [await backend.consume("a"), await backend.consume("b"), await backend.consume("a")]

    results = asyncio.run(scenario())

    assert results[0] is None
    assert results[1] is None
    assert results[2] is not None


def test_concurrent_callers_cannot_exceed_the_budget() -> None:
    # The check-and-record must stay atomic on the event loop even when callers interleave.
    backend = _backend(rate=3, per=60.0)

    async def scenario() -> list[float | None]:
        return list(await asyncio.gather(*(backend.consume("user") for _ in range(20))))

    results = asyncio.run(scenario())

    assert sum(result is None for result in results) == 3


def test_separate_backends_do_not_share_a_budget() -> None:
    # The per-process semantics a shared backend would replace: two processes, two budgets.
    first, second = _backend(rate=1), _backend(rate=1)

    async def scenario() -> tuple[float | None, float | None, float | None]:
        return await first.consume("u"), await second.consume("u"), await first.consume("u")

    allowed_first, allowed_second, refused = asyncio.run(scenario())

    assert (allowed_first, allowed_second) == (None, None)
    assert refused is not None


class _AlwaysBusy:
    """A stand-in for a shared backend, to show the bot depends only on the protocol."""

    rate = 5
    per = 30.0

    def __init__(self) -> None:
        self.keys: list[Hashable] = []

    async def consume(self, key: Hashable) -> float | None:
        self.keys.append(key)
        return 12.5


def test_any_object_with_the_protocol_can_be_the_bots_backend() -> None:
    from discord import app_commands
    from discord.ext import commands

    from aha.bot import AhaBot
    from aha.config import Settings
    from tests.helpers import (
        StubContext,
        StubInteraction,
        as_context,
        as_tree_interaction,
    )

    bot = AhaBot(Settings(token="test-token"))
    backend = _AlwaysBusy()
    bot.rate_limiter = backend

    async def scenario() -> tuple[commands.CommandOnCooldown, app_commands.CommandOnCooldown]:
        with pytest.raises(commands.CommandOnCooldown) as message_error:
            await bot._enforce_rate_limit(as_context(StubContext()))
        with pytest.raises(app_commands.CommandOnCooldown) as slash_error:
            await bot.tree.interaction_check(as_tree_interaction(StubInteraction()))
        return message_error.value, slash_error.value

    message_error, slash_error = asyncio.run(scenario())

    assert backend.keys == [33, 33]  # the user id, from both surfaces
    for error in (message_error, slash_error):
        assert error.retry_after == 12.5
        assert error.cooldown is not None
        assert (error.cooldown.rate, error.cooldown.per) == (5, 30.0)
