"""Tests for the gateway session-start budget and its guard."""

from __future__ import annotations

import asyncio
import logging

import pytest

from aha.gatewaybudget import DEFAULT_FLOOR, RESET_MARGIN, SessionBudget, await_budget


def budget(
    remaining: int, total: int = 1000, reset: float = 3600.0, conc: int = 1
) -> SessionBudget:
    return SessionBudget(total, remaining, reset, conc)


def test_the_payload_is_parsed_with_reset_after_in_seconds() -> None:
    parsed = SessionBudget.from_payload(
        {"total": 1000, "remaining": 998, "reset_after": 14_400_000, "max_concurrency": 16}
    )

    assert parsed == SessionBudget(1000, 998, 14_400.0, 16)


def test_a_missing_or_zero_max_concurrency_means_one_bucket() -> None:
    base = {"total": 1000, "remaining": 5, "reset_after": 1000}

    assert SessionBudget.from_payload(base).max_concurrency == 1
    assert SessionBudget.from_payload({**base, "max_concurrency": 0}).max_concurrency == 1


def test_the_default_floor_is_ten_percent() -> None:
    assert DEFAULT_FLOOR == 0.1


def test_the_floor_boundary_is_exclusive() -> None:
    assert budget(100).is_low(0.1) is False  # exactly 10 % is still allowed
    assert budget(99).is_low(0.1) is True
    assert budget(0).is_low(0.0) is False  # a floor of 0 never blocks


def test_a_healthy_budget_is_returned_without_waiting(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[SessionBudget] = []
    slept: list[float] = []

    async def fetch() -> SessionBudget:
        return budget(900, conc=4)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    with caplog.at_level(logging.INFO, logger="aha.gatewaybudget"):
        result = asyncio.run(await_budget(fetch, on_budget=seen.append, sleep=sleep))

    assert result == budget(900, conc=4)
    assert seen == [result]
    assert slept == []
    assert caplog.messages == [
        "Gateway session budget: 900 of 1000 remaining, resets in 3600 s, max concurrency 4"
    ]


def test_a_low_budget_waits_for_the_reset_instead_of_identifying(
    caplog: pytest.LogCaptureFixture,
) -> None:
    readings = [budget(50, reset=7200.0), budget(1000, reset=86_400.0)]
    seen: list[SessionBudget] = []
    slept: list[float] = []

    async def fetch() -> SessionBudget:
        return readings.pop(0)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    with caplog.at_level(logging.INFO, logger="aha.gatewaybudget"):
        result = asyncio.run(await_budget(fetch, floor=0.1, on_budget=seen.append, sleep=sleep))

    assert slept == [7200.0 + RESET_MARGIN]
    assert result.remaining == 1000  # looked again after the wait, and only then returned
    assert [b.remaining for b in seen] == [50, 1000]
    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert [r.getMessage() for r in critical] == [
        "Only 50 of 1000 gateway sessions remain (floor 10.0%): not identifying; "
        "waiting 7200 s for the daily reset"
    ]


def test_it_keeps_waiting_while_the_budget_stays_low() -> None:
    readings = [budget(1), budget(2), budget(3), budget(500)]
    slept: list[float] = []

    async def fetch() -> SessionBudget:
        return readings.pop(0)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    asyncio.run(await_budget(fetch, sleep=sleep))

    assert len(slept) == 3
    assert readings == []


def test_a_floor_of_zero_never_waits() -> None:
    slept: list[float] = []

    async def fetch() -> SessionBudget:
        return budget(0)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    assert asyncio.run(await_budget(fetch, floor=0.0, sleep=sleep)).remaining == 0
    assert slept == []


def test_the_default_floor_is_applied_when_none_is_given() -> None:
    readings = [budget(99), budget(100)]
    slept: list[float] = []

    async def fetch() -> SessionBudget:
        return readings.pop(0)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    asyncio.run(await_budget(fetch, sleep=sleep))

    assert len(slept) == 1
