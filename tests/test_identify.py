"""Tests for cross-pod IDENTIFY coordination, against a fake advisory-lock server."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from aha.identify import BASE_LOCK_ID, HOLD_SECONDS, IdentifyCoordinator


class LockServer:
    """One PostgreSQL's advisory locks, shared by every fake connection."""

    def __init__(self) -> None:
        self.held: set[int] = set()
        self.log: list[str] = []
        self.try_calls: list[int] = []
        self.fail_with: Exception | None = None
        self.unlock_fails = False


class FakeConnection:
    def __init__(self, server: LockServer, name: str) -> None:
        self.server = server
        self.name = name

    async def fetchval(self, query: str, *args: Any) -> Any:
        assert query == "SELECT pg_try_advisory_lock($1)"
        if self.server.fail_with is not None:
            raise self.server.fail_with
        (key,) = args
        self.server.try_calls.append(key)
        if key in self.server.held:
            return False
        self.server.held.add(key)
        self.server.log.append(f"lock {self.name} {key}")
        return True

    async def execute(self, query: str, *args: Any) -> str:
        assert query == "SELECT pg_advisory_unlock($1)"
        if self.server.unlock_fails:
            raise RuntimeError("connection lost")
        (key,) = args
        self.server.held.discard(key)
        self.server.log.append(f"unlock {self.name} {key}")
        return "OK"


class FakeDatabase:
    def __init__(self, server: LockServer, name: str = "a", fail: bool = False) -> None:
        self.server = server
        self.name = name
        self.fail = fail
        self.open = 0
        self.opened = 0

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[FakeConnection]:
        if self.fail:
            raise OSError("db down")
        self.open += 1
        self.opened += 1
        try:
            yield FakeConnection(self.server, self.name)
        finally:
            self.open -= 1


class Spawner:
    """Runs spawned release tasks like the task registry would, and remembers their names."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.tasks: list[asyncio.Task[None]] = []

    def __call__(self, factory: Callable[[], Awaitable[None]], name: str) -> None:
        self.names.append(name)
        self.tasks.append(asyncio.create_task(cast("Any", factory()), name=name))


def coordinator(
    db: FakeDatabase, spawner: Spawner, sleep: Callable[[float], Awaitable[None]], **kwargs: Any
) -> IdentifyCoordinator:
    return IdentifyCoordinator(cast("Any", db), spawner, sleep=sleep, **kwargs)


def test_the_lock_is_the_bucket_key_and_is_held_until_after_the_hold_time() -> None:
    server, spawner = LockServer(), Spawner()
    db = FakeDatabase(server)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    async def scenario() -> tuple[set[int], int]:
        await coordinator(db, spawner, sleep).acquire(3)
        state = (set(server.held), db.open)  # right after acquire: still held, connection kept
        await asyncio.gather(*spawner.tasks)
        return state

    held, open_connections = asyncio.run(scenario())

    assert held == {BASE_LOCK_ID + 3}
    assert open_connections == 1
    assert slept == [HOLD_SECONDS]
    assert server.log == [f"lock a {BASE_LOCK_ID + 3}", f"unlock a {BASE_LOCK_ID + 3}"]
    assert server.held == set()
    assert db.open == 0  # the connection went back to the pool
    assert spawner.names == ["identify-release-1"]


def test_the_default_hold_is_discords_five_seconds() -> None:
    assert HOLD_SECONDS == 5.0


def test_two_identifies_in_the_same_bucket_are_serialised_across_pods() -> None:
    server, spawner = LockServer(), Spawner()
    pod_a, pod_b = FakeDatabase(server, "a"), FakeDatabase(server, "b")

    async def sleep(seconds: float) -> None:
        await asyncio.sleep(0)  # let the other pod run

    async def scenario() -> None:
        first = coordinator(pod_a, spawner, sleep)
        second = coordinator(pod_b, spawner, sleep)
        await asyncio.gather(first.acquire(1), second.acquire(1))
        await asyncio.gather(*spawner.tasks)

    asyncio.run(scenario())

    key = BASE_LOCK_ID + 1
    # Whoever locked first must have unlocked before the other locked.
    assert [entry.split()[0] for entry in server.log] == ["lock", "unlock", "lock", "unlock"]
    assert server.log[0].endswith(str(key)) and server.log[2].endswith(str(key))
    assert len(spawner.names) == 2


def test_different_buckets_do_not_wait_for_each_other() -> None:
    server, spawner = LockServer(), Spawner()

    async def sleep(seconds: float) -> None:
        await asyncio.sleep(0)

    async def scenario() -> list[str]:
        await coordinator(FakeDatabase(server, "a"), spawner, sleep).acquire(0)
        await coordinator(FakeDatabase(server, "b"), spawner, sleep).acquire(1)
        log = list(server.log)  # both locked, nothing unlocked yet
        await asyncio.gather(*spawner.tasks)
        return log

    assert asyncio.run(scenario()) == [f"lock a {BASE_LOCK_ID}", f"lock b {BASE_LOCK_ID + 1}"]


def test_polling_gives_up_after_the_timeout_and_identifies_anyway(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, spawner = LockServer(), Spawner()
    server.held.add(BASE_LOCK_ID + 2)  # another pod holds it for good
    db = FakeDatabase(server)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    with caplog.at_level(logging.WARNING, logger="aha.identify"):
        asyncio.run(coordinator(db, spawner, sleep, poll=0.5, timeout=2.0).acquire(2))

    assert slept == [0.5, 0.5, 0.5, 0.5]
    assert len(server.try_calls) == 5  # the first try, then one after every sleep
    assert spawner.names == []
    assert db.open == 0
    assert caplog.messages == ["Could not take the identify lock for bucket 2; identifying anyway"]


def test_an_unreachable_database_does_not_block_identifying(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, spawner = LockServer(), Spawner()

    async def sleep(seconds: float) -> None:
        raise AssertionError("must not wait")

    with caplog.at_level(logging.WARNING, logger="aha.identify"):
        asyncio.run(coordinator(FakeDatabase(server, fail=True), spawner, sleep).acquire(0))

    assert spawner.names == []
    assert caplog.messages == ["Identify coordination unavailable; identifying without it"]
    assert isinstance(caplog.records[0].exc_info, tuple)


def test_a_failing_lock_query_releases_the_connection_and_identifies(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server, spawner = LockServer(), Spawner()
    server.fail_with = RuntimeError("boom")
    db = FakeDatabase(server)

    async def sleep(seconds: float) -> None:
        raise AssertionError("must not wait")

    with caplog.at_level(logging.WARNING, logger="aha.identify"):
        asyncio.run(coordinator(db, spawner, sleep).acquire(0))

    assert db.open == 0
    assert isinstance(caplog.records[0].exc_info, tuple)
    assert caplog.messages == [
        "Identify lock query failed; identifying without it",
        "Could not take the identify lock for bucket 0; identifying anyway",
    ]


def test_cancellation_while_waiting_releases_the_connection() -> None:
    server, spawner = LockServer(), Spawner()
    server.held.add(BASE_LOCK_ID)
    db = FakeDatabase(server)

    async def sleep(seconds: float) -> None:
        await asyncio.Event().wait()

    async def scenario() -> None:
        task = asyncio.create_task(coordinator(db, spawner, sleep).acquire(0))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert db.open == 0


def test_release_returns_the_connection_even_if_unlocking_fails() -> None:
    server, spawner = LockServer(), Spawner()
    server.unlock_fails = True
    db = FakeDatabase(server)

    async def sleep(seconds: float) -> None:
        return None

    async def scenario() -> list[object]:
        await coordinator(db, spawner, sleep).acquire(0)
        return await asyncio.gather(*spawner.tasks, return_exceptions=True)

    (outcome,) = asyncio.run(scenario())

    assert isinstance(outcome, RuntimeError)
    assert db.open == 0


def test_each_release_task_gets_its_own_name() -> None:
    server, spawner = LockServer(), Spawner()

    async def sleep(seconds: float) -> None:
        return None

    async def scenario() -> None:
        c = coordinator(FakeDatabase(server), spawner, sleep)
        await c.acquire(0)
        await asyncio.gather(*spawner.tasks)
        await c.acquire(0)
        await asyncio.gather(*spawner.tasks)

    asyncio.run(scenario())

    assert spawner.names == ["identify-release-1", "identify-release-2"]


def test_the_defaults_poll_twice_a_second_and_give_up_after_a_minute() -> None:
    coordinator = IdentifyCoordinator(cast("Any", FakeDatabase(LockServer())), Spawner())

    assert (coordinator._poll, coordinator._timeout, coordinator._hold) == (0.5, 60.0, 5.0)
