"""Tests for the per-user token-bucket limiter."""

from __future__ import annotations

from aha.ratelimit import RateLimiter


class _Clock:
    """Deterministic clock for refill tests."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_rate_then_reports_the_wait() -> None:
    clock = _Clock()
    limiter = RateLimiter(2, 10.0, clock=clock)

    assert limiter.consume(1) is None
    assert limiter.consume(1) is None
    retry_after = limiter.consume(1)
    assert retry_after is not None
    assert 0 < retry_after <= 10.0


def test_tokens_refill_after_the_window() -> None:
    clock = _Clock()
    limiter = RateLimiter(1, 10.0, clock=clock)
    assert limiter.consume(1) is None
    assert limiter.consume(1) is not None

    clock.now += 11.0

    assert limiter.consume(1) is None


def test_buckets_are_per_key() -> None:
    clock = _Clock()
    limiter = RateLimiter(1, 10.0, clock=clock)

    assert limiter.consume(1) is None
    assert limiter.consume(2) is None
    assert limiter.consume(1) is not None
