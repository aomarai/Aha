"""Unit tests for the scheduler's polling, retry and dead-letter logic (a fake store)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from aha.jobstore import Job
from aha.scheduler import DEAD_RETENTION, MAX_BACKOFF, Scheduler

NOW = datetime(2026, 11, 13, tzinfo=UTC)


def _job(job_id: int = 1, kind: str = "reminder", attempts: int = 1) -> Job:
    return Job(job_id, kind, NOW, 33, 11, {"text": "hi"}, attempts)


class FakeStore:
    """Hands out preset jobs once and records what the scheduler does with them."""

    def __init__(self, *jobs: Job, lag: float = 0.0, purged: int = 0) -> None:
        self.jobs = list(jobs)
        self._lag = lag
        self._purged = purged
        self.claims: list[tuple[int, float]] = []
        self.completed: list[int] = []
        self.failed: list[tuple[int, str, float, bool]] = []
        self.purge_calls: list[float] = []

    async def claim(self, limit: int, lease_seconds: float) -> list[Job]:
        self.claims.append((limit, lease_seconds))
        jobs, self.jobs = self.jobs, []
        return jobs

    async def complete(self, job_id: int) -> None:
        self.completed.append(job_id)

    async def fail(self, job_id: int, error: str, *, retry_in: float, dead: bool) -> None:
        self.failed.append((job_id, error, retry_in, dead))

    async def lag(self) -> float:
        return self._lag

    async def purge_dead(self, older_than_seconds: float) -> int:
        self.purge_calls.append(older_than_seconds)
        return self._purged


def _run_once(scheduler: Scheduler) -> int:
    return asyncio.run(scheduler.run_once())


def test_a_job_is_run_by_its_kind_and_then_deleted() -> None:
    seen: list[Job] = []

    async def handler(job: Job) -> None:
        seen.append(job)

    store = FakeStore(_job(7))
    scheduler = Scheduler(store)
    scheduler.register("reminder", handler)

    assert _run_once(scheduler) == 1

    assert [job.id for job in seen] == [7]
    assert store.completed == [7]
    assert store.failed == []


def test_each_kind_goes_to_its_own_handler() -> None:
    calls: list[str] = []

    async def reminder(job: Job) -> None:
        calls.append(f"reminder:{job.id}")

    async def poll(job: Job) -> None:
        calls.append(f"poll:{job.id}")

    store = FakeStore(_job(1, "reminder"), _job(2, "poll"))
    scheduler = Scheduler(store)
    scheduler.register("reminder", reminder)
    scheduler.register("poll", poll)

    _run_once(scheduler)

    assert calls == ["reminder:1", "poll:2"]
    assert store.completed == [1, 2]


def test_the_batch_size_and_lease_are_passed_to_the_store() -> None:
    store = FakeStore()

    _run_once(Scheduler(store, batch=4, lease=90.0))

    assert store.claims == [(4, 90.0)]


def test_a_failing_handler_is_retried_with_backoff_and_the_error_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom(job: Job) -> None:
        raise ValueError("kaput")

    store = FakeStore(_job(5, attempts=2))
    scheduler = Scheduler(store, backoff=30.0, max_attempts=5)
    scheduler.register("reminder", boom)

    with caplog.at_level(logging.ERROR, logger="aha.scheduler"):
        _run_once(scheduler)

    assert store.completed == []
    assert store.failed == [(5, "ValueError: kaput", 60.0, False)]  # attempt 2 -> 30 * 2
    assert [r.getMessage() for r in caplog.records] == [
        "Scheduled job 5 (reminder) failed on attempt 2"
    ]


def test_a_job_on_its_last_attempt_is_marked_dead() -> None:
    async def boom(job: Job) -> None:
        raise RuntimeError("still broken")

    store = FakeStore(_job(1, attempts=4), _job(2, attempts=5), _job(3, attempts=6))
    scheduler = Scheduler(store, max_attempts=5)
    scheduler.register("reminder", boom)

    _run_once(scheduler)

    assert [(job_id, dead) for job_id, _, _, dead in store.failed] == [
        (1, False),
        (2, True),  # exactly max_attempts
        (3, True),
    ]


def test_giving_up_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    async def boom(job: Job) -> None:
        raise RuntimeError("x")

    scheduler = Scheduler(FakeStore(_job(9, attempts=3)), max_attempts=3)
    scheduler.register("reminder", boom)

    with caplog.at_level(logging.ERROR, logger="aha.scheduler"):
        _run_once(scheduler)

    assert caplog.records[0].getMessage() == (
        "Scheduled job 9 (reminder) failed on attempt 3; giving up"
    )


def test_an_unknown_kind_is_retried_not_lost() -> None:
    store = FakeStore(_job(4, "from-a-newer-release", attempts=1))

    _run_once(Scheduler(store, backoff=10.0))

    assert store.failed == [
        (4, "LookupError: no handler registered for 'from-a-newer-release'", 10.0, False)
    ]


def test_a_handler_that_overruns_its_timeout_is_failed() -> None:
    async def slow(job: Job) -> None:
        await asyncio.sleep(5)

    store = FakeStore(_job(1))
    scheduler = Scheduler(store, handler_timeout=0.05, lease=1.0)
    scheduler.register("reminder", slow)

    _run_once(scheduler)

    ((job_id, error, _, dead),) = store.failed
    assert (job_id, error, dead) == (1, "TimeoutError: ", False)


def test_a_long_error_message_is_truncated() -> None:
    async def boom(job: Job) -> None:
        raise ValueError("x" * 2000)

    store = FakeStore(_job(1))
    scheduler = Scheduler(store)
    scheduler.register("reminder", boom)

    _run_once(scheduler)

    assert len(store.failed[0][1]) == 500
    assert store.failed[0][1].startswith("ValueError: xxx")


def test_one_failing_job_does_not_stop_the_rest_of_the_batch() -> None:
    async def sometimes(job: Job) -> None:
        if job.id == 1:
            raise ValueError("no")

    store = FakeStore(_job(1), _job(2))
    scheduler = Scheduler(store)
    scheduler.register("reminder", sometimes)

    _run_once(scheduler)

    assert [f[0] for f in store.failed] == [1]
    assert store.completed == [2]


def test_cancellation_is_not_treated_as_a_failure() -> None:
    async def cancelled(job: Job) -> None:
        raise asyncio.CancelledError

    store = FakeStore(_job(1))
    scheduler = Scheduler(store)
    scheduler.register("reminder", cancelled)

    with pytest.raises(asyncio.CancelledError):
        _run_once(scheduler)

    assert store.failed == []  # the lease simply expires and another pod runs it
    assert store.completed == []


@pytest.mark.parametrize(
    ("attempts", "delay"),
    [(1, 30.0), (2, 60.0), (3, 120.0), (4, 240.0), (8, 3600.0), (50, 3600.0)],
)
def test_the_retry_delay_doubles_and_is_capped(attempts: int, delay: float) -> None:
    assert Scheduler(FakeStore(), backoff=30.0).retry_delay(attempts) == delay
    assert MAX_BACKOFF == 3600.0


def test_the_lag_is_reported_after_each_poll() -> None:
    lags: list[float] = []

    _run_once(Scheduler(FakeStore(lag=42.5), on_lag=lags.append))

    assert lags == [42.5]


def test_no_lag_callback_is_fine() -> None:
    assert _run_once(Scheduler(FakeStore(lag=1.0))) == 0


def test_long_dead_jobs_are_purged_each_poll_and_the_purge_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = FakeStore(purged=3)

    with caplog.at_level(logging.INFO, logger="aha.scheduler"):
        _run_once(Scheduler(store))

    assert store.purge_calls == [DEAD_RETENTION]
    assert DEAD_RETENTION == 7 * 24 * 3600
    assert [r.getMessage() for r in caplog.records] == ["Deleted 3 long-dead scheduled jobs"]


def test_nothing_is_logged_when_nothing_was_purged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="aha.scheduler"):
        _run_once(Scheduler(FakeStore(purged=0)))

    assert caplog.records == []


def test_a_kind_can_only_be_registered_once() -> None:
    scheduler = Scheduler(FakeStore())

    async def handler(job: Job) -> None:
        return None

    scheduler.register("reminder", handler)

    with pytest.raises(ValueError, match="a handler for 'reminder' is already registered"):
        scheduler.register("reminder", handler)


def test_the_lease_must_outlast_the_handler_timeout() -> None:
    message = "^lease must be longer than handler_timeout, or jobs would run twice$"

    with pytest.raises(ValueError, match=message):
        Scheduler(FakeStore(), lease=30.0, handler_timeout=30.0)
    with pytest.raises(ValueError, match=message):
        Scheduler(FakeStore(), lease=10.0, handler_timeout=30.0)
    Scheduler(FakeStore(), lease=30.1, handler_timeout=30.0)  # just enough


def test_run_polls_repeatedly_and_sleeps_between_polls() -> None:
    store = FakeStore(_job(1), _job(2))
    sleeps: list[float] = []

    class StopPollingError(Exception):
        pass

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 3:
            raise StopPollingError

    async def handler(job: Job) -> None:
        return None

    scheduler = Scheduler(store, poll_interval=2.5, sleep=fake_sleep)
    scheduler.register("reminder", handler)

    with pytest.raises(StopPollingError):
        asyncio.run(scheduler.run())

    assert sleeps == [2.5, 2.5, 2.5]
    assert len(store.claims) == 3  # one claim per poll
    assert store.completed == [1, 2]


def test_the_default_sleep_is_asyncio_sleep() -> None:
    scheduler: Any = Scheduler(FakeStore())

    assert scheduler._sleep is asyncio.sleep


def test_the_defaults_are_the_documented_ones() -> None:
    store = FakeStore()
    sleeps: list[float] = []

    class StopPollingError(Exception):
        pass

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        raise StopPollingError

    scheduler = Scheduler(store, sleep=fake_sleep)

    with pytest.raises(StopPollingError):
        asyncio.run(scheduler.run())

    assert sleeps == [5.0]  # poll every 5 seconds
    assert store.claims == [(10, 60.0)]  # batches of 10, with a 60 second lease


def test_the_default_handler_timeout_is_thirty_seconds_and_backoff_starts_at_thirty() -> None:
    async def slow(job: Job) -> None:
        await asyncio.sleep(0)

    scheduler: Any = Scheduler(FakeStore())

    assert scheduler._handler_timeout == 30.0
    assert scheduler.retry_delay(1) == 30.0
    assert scheduler._max_attempts == 5
