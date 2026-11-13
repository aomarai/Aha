"""Tests for the supervised background task registry."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import pytest

from aha import logctx
from aha.tasks import STABLE_AFTER, TaskRegistry
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

    calls: list[str] = []

    async def failing() -> None:
        calls.append("ran")
        raise _BoomError

    async def scenario() -> None:
        await registry.spawn(failing, name="optional")

    asyncio.run(scenario())

    assert calls == ["ran"]  # it ran once, failed, and was not restarted
    assert registry.healthy is True
    assert registry.failed == frozenset()


def test_a_task_that_returns_normally_is_forgotten_without_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _registry()

    calls: list[str] = []

    async def finishes() -> None:
        calls.append("ran")

    async def scenario() -> bool:
        await registry.spawn(finishes, name="once")
        await asyncio.sleep(0)
        return "once" in registry

    with caplog.at_level(logging.DEBUG, logger="aha.tasks"):
        assert asyncio.run(scenario()) is False

    assert calls == ["ran"]
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
    assert runs == 2  # failed once, then the restart is the run that is still going


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
        with pytest.raises(RuntimeError, match="the task registry is closed"):
            registry.spawn(closer, name="after")

    asyncio.run(scenario())

    assert outcome == ["closed without cancelling itself"]
    assert registry.closed is True


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


def test_a_new_registry_is_open_and_close_marks_it_closed() -> None:
    registry = _registry()

    assert registry.closed is False

    asyncio.run(registry.close())

    assert registry.closed is True


def test_backoff_resets_when_a_run_lasts_exactly_the_cap() -> None:
    sleeps: list[float] = []
    clock = FakeClock(0.0)
    registry = _registry(sleeps, clock, initial_backoff=1.0, max_backoff=60.0)
    runs = 0

    async def flaky() -> None:
        nonlocal runs
        runs += 1
        if runs == 3:
            clock.now += 60.0  # exactly max_backoff counts as a healthy run
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


def test_a_restartable_task_is_not_restarted_once_the_registry_is_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleeps: list[float] = []
    registry = _registry(sleeps)
    runs = 0

    async def fails_while_unwinding() -> None:
        nonlocal runs
        runs += 1
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise _BoomError from None  # an ordinary exception during cancellation

    async def scenario() -> None:
        registry.spawn(fails_while_unwinding, name="unwinder", restart=True)
        await asyncio.sleep(0)
        await registry.close()

    with caplog.at_level(logging.WARNING, logger="aha.tasks"):
        asyncio.run(scenario())

    assert runs == 1
    assert sleeps == []
    assert [r.getMessage() for r in caplog.records] == ["Background task unwinder failed"]
    assert len(registry) == 0


def test_cancelling_a_critical_task_while_open_marks_the_registry_unhealthy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = _registry()

    async def scenario() -> tuple[bool, frozenset[str]]:
        task = registry.spawn(_forever, name="vital", critical=True)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait({task})
        return registry.healthy, registry.failed

    with caplog.at_level(logging.ERROR, logger="aha.tasks"):
        healthy, failed = asyncio.run(scenario())

    assert healthy is False
    assert failed == frozenset({"vital"})
    assert [r.getMessage() for r in caplog.records] == [
        "Critical background task vital was cancelled"
    ]


def test_closing_the_registry_does_not_mark_critical_tasks_failed() -> None:
    registry = _registry()

    async def scenario() -> None:
        registry.spawn(_forever, name="vital", critical=True)
        await asyncio.sleep(0)
        await registry.close()

    asyncio.run(scenario())

    assert registry.healthy is True


def test_cancelling_a_non_critical_task_leaves_the_registry_healthy() -> None:
    registry = _registry()

    async def scenario() -> None:
        task = registry.spawn(_forever, name="optional")
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait({task})

    asyncio.run(scenario())

    assert registry.healthy is True


def test_respawning_a_failed_critical_task_restores_health() -> None:
    registry = _registry()
    runs: list[str] = []

    async def fails_then_works() -> None:
        runs.append("run")
        if len(runs) == 1:
            raise _BoomError
        await asyncio.sleep(3600)

    async def scenario() -> tuple[bool, bool]:
        await registry.spawn(fails_then_works, name="vital", critical=True)
        unhealthy_after_crash = not registry.healthy
        registry.spawn(fails_then_works, name="vital", critical=True)
        await asyncio.sleep(0)
        recovered = registry.healthy
        await registry.close()
        return unhealthy_after_crash, recovered

    assert asyncio.run(scenario()) == (True, True)
    assert len(runs) == 2


def test_a_cancelled_task_that_closes_in_cleanup_does_not_cancel_the_first_closer() -> None:
    registry = _registry()
    finished: list[str] = []

    async def scenario() -> None:
        gate = asyncio.Event()

        async def stubborn() -> None:
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    continue  # ignore cancellation until released

        async def first_closer() -> None:
            await registry.close(timeout=5.0)
            finished.append("first")

        async def cleanup_closer() -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # Cancelled by the first closer; its cleanup closes the registry too.
                await registry.close(timeout=5.0)
                finished.append("cleanup")

        registry.spawn(stubborn, name="stubborn")
        cleanup = registry.spawn(cleanup_closer, name="cleanup")  # must be running first
        first = registry.spawn(first_closer, name="first")
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        await asyncio.wait({first, cleanup})

    asyncio.run(scenario())

    assert sorted(finished) == ["cleanup", "first"]


async def _forever() -> None:
    await asyncio.sleep(3600)


def test_a_spawned_task_does_not_inherit_the_invocation_log_context() -> None:
    registry = _registry()
    seen: list[dict[str, object]] = []

    async def background() -> None:
        seen.append(dict(logctx.current()))

    async def scenario() -> dict[str, object]:
        logctx.bind(user_id=5, command="remind")  # as if spawned from inside a command
        await registry.spawn(background, name="job")
        return dict(logctx.current())

    assert asyncio.run(scenario()) == {"user_id": 5, "command": "remind"}
    assert seen == [{}]


def test_closing_does_not_leave_the_caller_registered_as_a_closer() -> None:
    registry = _registry()

    async def scenario() -> None:
        await registry.close()

    asyncio.run(scenario())

    assert registry._closers == set()


def _flaky_then_stable(fail_times: int) -> tuple[list[int], Callable[[], Awaitable[None]]]:
    runs: list[int] = []

    async def run() -> None:
        runs.append(len(runs) + 1)
        if len(runs) <= fail_times:
            raise _BoomError
        await asyncio.sleep(3600)

    return runs, run


def test_a_restarting_critical_task_is_degraded_but_not_failed() -> None:
    registry = _registry()
    _, run = _flaky_then_stable(fail_times=1)

    async def scenario() -> tuple[frozenset[str], frozenset[str], bool]:
        registry.spawn(run, name="vital", restart=True, critical=True)
        for _ in range(10):
            await asyncio.sleep(0)
        result = (registry.degraded, registry.failed, registry.healthy)
        await registry.close()
        return result

    degraded, failed, healthy = asyncio.run(scenario())

    assert degraded == frozenset({"vital"})  # restarted, but has not run stably yet
    assert failed == frozenset()
    assert healthy is True


def test_a_critical_task_is_degraded_while_it_waits_to_restart() -> None:
    gate: list[asyncio.Event] = []

    async def parked_sleep(delay: float) -> None:
        event = asyncio.Event()
        gate.append(event)
        await event.wait()

    registry = TaskRegistry(sleep=parked_sleep, clock=FakeClock())

    async def fails() -> None:
        raise _BoomError

    async def scenario() -> frozenset[str]:
        registry.spawn(fails, name="vital", restart=True, critical=True)
        for _ in range(5):
            await asyncio.sleep(0)
        result = registry.degraded  # parked in back-off, not running
        await registry.close()
        return result

    assert asyncio.run(scenario()) == frozenset({"vital"})


def test_a_restarted_critical_task_recovers_after_running_stably() -> None:
    clock = FakeClock(0.0)
    registry = _registry(clock=clock)
    _, run = _flaky_then_stable(fail_times=1)

    async def scenario() -> list[frozenset[str]]:
        registry.spawn(run, name="vital", restart=True, critical=True)
        for _ in range(10):
            await asyncio.sleep(0)
        seen = [registry.degraded]
        clock.now += STABLE_AFTER - 0.1
        seen.append(registry.degraded)
        clock.now += 0.1  # exactly STABLE_AFTER since the restarted run began
        seen.append(registry.degraded)
        await registry.close()
        return seen

    assert asyncio.run(scenario()) == [
        frozenset({"vital"}),
        frozenset({"vital"}),
        frozenset(),
    ]


def test_a_non_critical_restarting_task_is_never_degraded() -> None:
    registry = _registry()
    _, run = _flaky_then_stable(fail_times=1)

    async def scenario() -> frozenset[str]:
        registry.spawn(run, name="optional", restart=True)
        for _ in range(10):
            await asyncio.sleep(0)
        result = registry.degraded
        await registry.close()
        return result

    assert asyncio.run(scenario()) == frozenset()


def test_closing_clears_the_degraded_state() -> None:
    registry = _registry()
    _, run = _flaky_then_stable(fail_times=1)

    async def scenario() -> None:
        registry.spawn(run, name="vital", restart=True, critical=True)
        for _ in range(10):
            await asyncio.sleep(0)
        await registry.close()

    asyncio.run(scenario())

    assert registry.degraded == frozenset()
