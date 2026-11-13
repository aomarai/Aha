"""Tests for the supervised background task registry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest

from aha.tasks import TaskRegistry
from tests.helpers import FakeClock


class _BoomError(Exception):
    pass


def _registry(
    sleeps: list[float] | None = None, clock: FakeClock | None = None, **kwargs: float
) -> TaskRegistry:
    recorded = sleeps if sleeps is not None else []

    async def fake_sleep(delay: float) -> None:
        recorded.append(delay)
        await asyncio.sleep(0)

    return TaskRegistry(sleep=fake_sleep, clock=clock or FakeClock(), **kwargs)


def test_a_failing_task_is_logged_with_its_name_and_the_loop_survives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _registry()
    survivor: list[str] = []

    async def failing() -> None:
        raise _BoomError("kaput")

    async def healthy() -> None:
        survivor.append("ran")

    async def scenario() -> None:
        task = registry.spawn(failing, name="flaky")
        await task
        await registry.spawn(healthy, name="steady")

    with caplog.at_level(logging.ERROR, logger="aha.tasks"):
        asyncio.run(scenario())

    assert survivor == ["ran"]
    assert [record.getMessage() for record in caplog.records] == ["Background task flaky failed"]
    assert caplog.records[0].exc_info is not None
    assert caplog.records[0].exc_info[0] is _BoomError
    assert len(registry) == 0


def test_a_critical_task_that_dies_marks_the_registry_unhealthy() -> None:
    registry = _registry()

    async def failing() -> None:
        raise _BoomError

    async def scenario() -> tuple[bool, frozenset[str]]:
        await registry.spawn(failing, name="vital", critical=True)
        return registry.healthy, registry.failed

    healthy, failed = asyncio.run(scenario())

    assert healthy is False
    assert failed == frozenset({"vital"})


def test_a_non_critical_failure_leaves_the_registry_healthy() -> None:
    registry = _registry()

    async def failing() -> None:
        raise _BoomError

    async def scenario() -> None:
        await registry.spawn(failing, name="optional")

    asyncio.run(scenario())

    assert registry.healthy is True
    assert registry.failed == frozenset()


def test_a_task_that_returns_normally_is_forgotten_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _registry()

    async def finishes() -> None:
        return None

    async def scenario() -> bool:
        await registry.spawn(finishes, name="once")
        await asyncio.sleep(0)
        return "once" in registry

    with caplog.at_level(logging.DEBUG, logger="aha.tasks"):
        assert asyncio.run(scenario()) is False

    assert caplog.records == []
    assert registry.healthy is True


def test_a_restartable_task_restarts_with_doubling_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleeps: list[float] = []
    registry = _registry(sleeps, initial_backoff=1.0, max_backoff=5.0)
    attempts: list[int] = []

    async def flaky() -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) < 5:
            raise _BoomError
        await asyncio.sleep(3600)  # healthy from the fifth attempt on

    async def scenario() -> None:
        registry.spawn(flaky, name="loop", restart=True)
        for _ in range(20):
            await asyncio.sleep(0)
        await registry.close()

    with caplog.at_level(logging.WARNING, logger="aha.tasks"):
        asyncio.run(scenario())

    assert attempts == [1, 2, 3, 4, 5]
    assert sleeps == [1.0, 2.0, 4.0, 5.0]  # capped at max_backoff
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        "Restarting background task loop in 1.0 s",
        "Restarting background task loop in 2.0 s",
        "Restarting background task loop in 4.0 s",
        "Restarting background task loop in 5.0 s",
    ]


def test_backoff_resets_after_a_long_healthy_run() -> None:
    sleeps: list[float] = []
    clock = FakeClock(0.0)
    registry = _registry(sleeps, clock, initial_backoff=1.0, max_backoff=60.0)
    runs = 0

    async def flaky() -> None:
        nonlocal runs
        runs += 1
        if runs == 3:
            clock.now += 61.0  # this run survived longer than max_backoff
        if runs < 4:
            raise _BoomError
        await asyncio.sleep(3600)

    async def scenario() -> None:
        registry.spawn(flaky, name="loop", restart=True)
        for _ in range(20):
            await asyncio.sleep(0)
        await registry.close()

    asyncio.run(scenario())

    assert sleeps == [1.0, 2.0, 1.0]


def test_a_restarted_task_is_not_marked_failed() -> None:
    registry = _registry()
    runs = 0

    async def flaky() -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise _BoomError
        await asyncio.sleep(3600)

    async def scenario() -> bool:
        registry.spawn(flaky, name="loop", restart=True, critical=True)
        for _ in range(10):
            await asyncio.sleep(0)
        healthy = registry.healthy
        await registry.close()
        return healthy

    assert asyncio.run(scenario()) is True


def test_close_cancels_every_task_and_refuses_new_ones() -> None:
    registry = _registry()
    cancelled: list[str] = []

    def sleeper(name: str) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(name)
                raise

        return run

    async def scenario() -> None:
        registry.spawn(sleeper("a"), name="a")
        registry.spawn(sleeper("b"), name="b")
        await asyncio.sleep(0)
        await registry.close()
        await registry.close()  # idempotent
        with pytest.raises(RuntimeError, match=r"cannot spawn 'c': the task registry is closed"):
            registry.spawn(sleeper("c"), name="c")

    asyncio.run(scenario())

    assert sorted(cancelled) == ["a", "b"]
    assert len(registry) == 0


def test_spawning_a_duplicate_live_name_is_rejected() -> None:
    registry = _registry()

    async def forever() -> None:
        await asyncio.sleep(3600)

    async def scenario() -> None:
        registry.spawn(forever, name="dup")
        with pytest.raises(ValueError, match=r"^a task named 'dup' is already running$"):
            registry.spawn(forever, name="dup")
        await registry.close()

    asyncio.run(scenario())


def test_a_finished_name_can_be_reused() -> None:
    registry = _registry()

    async def finishes() -> None:
        return None

    async def scenario() -> None:
        await registry.spawn(finishes, name="again")
        await asyncio.sleep(0)
        await registry.spawn(finishes, name="again")

    asyncio.run(scenario())

    assert len(registry) == 0


def test_a_task_can_close_the_registry_it_belongs_to() -> None:
    registry = _registry()
    outcome: list[str] = []

    async def closer() -> None:
        await registry.close()
        outcome.append("closed without cancelling itself")

    async def scenario() -> None:
        await registry.spawn(closer, name="closer")

    asyncio.run(scenario())

    assert outcome == ["closed without cancelling itself"]


def test_close_warns_about_a_task_that_ignores_cancellation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _registry()

    async def scenario() -> None:
        gate = asyncio.Event()

        async def stubborn() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await gate.wait()  # keep unwinding until released, as a stuck task would

        task = registry.spawn(stubborn, name="stubborn")
        await asyncio.sleep(0)
        await registry.close(timeout=0.01)
        gate.set()
        await task

    with caplog.at_level(logging.WARNING, logger="aha.tasks"):
        asyncio.run(scenario())

    assert [record.getMessage() for record in caplog.records] == [
        "Background task stubborn did not stop within 0.0 s"
    ]


@pytest.mark.parametrize(
    ("initial", "maximum"),
    [
        (0.0, 60.0),
        (-1.0, 60.0),
        (float("nan"), 60.0),
        (1.0, float("inf")),
        (5.0, 1.0),
    ],
)
def test_invalid_backoff_values_are_rejected(initial: float, maximum: float) -> None:
    with pytest.raises(
        ValueError, match=r"^backoff values must be finite, positive, and max >= initial$"
    ):
        TaskRegistry(initial_backoff=initial, max_backoff=maximum)


def test_valid_equal_backoff_bounds_are_accepted() -> None:
    registry = TaskRegistry(initial_backoff=2.0, max_backoff=2.0)

    assert len(registry) == 0
