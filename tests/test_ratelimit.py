"""Tests for the per-user sliding-window limiter."""

from __future__ import annotations

import pytest

from aha.ratelimit import RateLimiter
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
