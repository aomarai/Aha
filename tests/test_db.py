"""Unit tests for the database pool wrapper (the real pool is faked)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, cast

import asyncpg
import pytest

from aha import db as db_module
from aha.db import APPLICATION_NAME, CONNECT_WAIT, Database, connect_once


class FakeConnection:
    def __init__(self, answer: Any = 1) -> None:
        self.answer = answer
        self.queries: list[str] = []

    async def fetchval(self, query: str) -> Any:
        self.queries.append(query)
        return self.answer


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.closed = False
        self.acquired = 0

    @asynccontextmanager
    async def acquire(self) -> Any:
        self.acquired += 1
        yield self.connection

    async def close(self) -> None:
        self.closed = True


def _database(answer: Any = 1) -> tuple[Database, FakePool]:
    pool = FakePool(FakeConnection(answer))
    return Database(cast("Any", pool)), pool


def test_connect_opens_a_small_pool_with_a_server_side_statement_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    pool = FakePool(FakeConnection())

    async def fake_create_pool(*args: Any, **kwargs: Any) -> FakePool:
        calls.append((args, kwargs))
        return pool

    monkeypatch.setattr("aha.db.asyncpg.create_pool", fake_create_pool)

    database = asyncio.run(
        Database.connect("postgresql://u:p@h/db", max_size=7, statement_timeout=1.5)
    )

    assert calls == [
        (
            ("postgresql://u:p@h/db",),
            {
                "min_size": 1,
                "max_size": 7,
                "server_settings": {"application_name": "aha", "statement_timeout": "1500"},
            },
        )
    ]
    assert isinstance(database, Database)
    assert asyncio.run(database.ping()) is True  # it wraps the pool that was created
    assert pool.acquired == 1
    assert APPLICATION_NAME == "aha"


@pytest.mark.parametrize(("seconds", "millis"), [(2.0, "2000"), (0.25, "250"), (0.0004, "0")])
def test_the_statement_timeout_is_sent_in_whole_milliseconds(
    monkeypatch: pytest.MonkeyPatch, seconds: float, millis: str
) -> None:
    seen: list[dict[str, Any]] = []

    async def fake_create_pool(*args: Any, **kwargs: Any) -> FakePool:
        seen.append(kwargs["server_settings"])
        return FakePool(FakeConnection())

    monkeypatch.setattr("aha.db.asyncpg.create_pool", fake_create_pool)

    asyncio.run(Database.connect("postgresql://h/db", max_size=1, statement_timeout=seconds))

    assert seen[0]["statement_timeout"] == millis


def test_acquire_lends_a_pooled_connection() -> None:
    database, pool = _database()

    async def scenario() -> Any:
        async with database.acquire() as connection:
            return connection

    assert asyncio.run(scenario()) is pool.connection
    assert pool.acquired == 1


def test_ping_is_true_when_the_database_answers_one() -> None:
    database, pool = _database(answer=1)

    assert asyncio.run(database.ping()) is True
    assert pool.connection.queries == ["SELECT 1"]


def test_ping_is_false_for_any_other_answer() -> None:
    database, _ = _database(answer=0)

    assert asyncio.run(database.ping()) is False


def test_close_closes_the_pool() -> None:
    database, pool = _database()

    asyncio.run(database.close())

    assert pool.closed is True


def test_connect_once_opens_one_connection_tagged_as_a_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    sentinel = object()

    async def fake_connect(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr("aha.db.asyncpg.connect", fake_connect)

    result = asyncio.run(connect_once("postgresql://h/db"))

    assert result is sentinel
    assert calls == [(("postgresql://h/db",), {"server_settings": {"application_name": "aha-job"}})]


# ---------------------------------------------------------------- waiting for the database


class Clock:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Flaky:
    """An operation that raises ``error`` the first ``times`` calls, then succeeds."""

    def __init__(self, times: int, error: Exception) -> None:
        self.times = times
        self.error = error
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.times:
            raise self.error
        return "connected"


def retry(connect: Flaky, clock: Clock, wait: float) -> str:
    result: str = asyncio.run(
        db_module._retrying(connect, wait=wait, sleep=clock.sleep, clock=clock)
    )
    return result


def test_the_default_wait_is_a_minute() -> None:
    assert CONNECT_WAIT == 60.0


def test_a_database_that_is_not_reachable_yet_is_retried_until_it_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    flaky = Flaky(3, ConnectionRefusedError("refused"))

    with caplog.at_level(logging.WARNING, logger="aha.db"):
        assert retry(flaky, clock, wait=60.0) == "connected"

    assert flaky.calls == 4
    assert clock.slept == [2.0, 2.0, 2.0]
    assert caplog.messages == [
        "Database not reachable yet (attempt 1, ConnectionRefusedError); retrying in 2 s",
        "Database not reachable yet (attempt 2, ConnectionRefusedError); retrying in 2 s",
        "Database not reachable yet (attempt 3, ConnectionRefusedError); retrying in 2 s",
    ]


def test_it_gives_up_when_the_wait_is_used_up_and_raises_the_last_error() -> None:
    clock = Clock()
    flaky = Flaky(100, OSError("unreachable"))

    with pytest.raises(OSError, match="unreachable"):
        retry(flaky, clock, wait=6.0)

    # tries at 0, 2, 4 and 6 s; after the fourth, another 2 s wait would pass the deadline
    assert flaky.calls == 4
    assert clock.slept == [2.0, 2.0, 2.0]


def test_the_deadline_is_inclusive_of_the_last_wait_that_fits() -> None:
    clock = Clock()

    with pytest.raises(OSError):
        retry(Flaky(100, OSError("x")), clock, wait=4.0)

    assert clock.slept == [2.0, 2.0]  # a wait that ends exactly at the deadline is still taken


def test_a_zero_wait_never_retries() -> None:
    clock = Clock()
    flaky = Flaky(1, OSError("down"))

    with pytest.raises(OSError):
        retry(flaky, clock, wait=0.0)

    assert (flaky.calls, clock.slept) == (1, [])


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.CannotConnectNowError("starting up"),
        asyncpg.ConnectionDoesNotExistError("gone"),
        TimeoutError("slow"),
    ],
)
def test_the_other_not_reachable_yet_errors_are_retried_too(error: Exception) -> None:
    clock = Clock()

    assert retry(Flaky(1, error), clock, wait=60.0) == "connected"
    assert clock.slept == [2.0]


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.InvalidPasswordError("bad password"),
        asyncpg.InvalidCatalogNameError("no such database"),
        ValueError("bad url"),
    ],
)
def test_errors_that_will_not_fix_themselves_fail_at_once(error: Exception) -> None:
    clock = Clock()
    flaky = Flaky(100, error)

    with pytest.raises(type(error)):
        retry(flaky, clock, wait=60.0)

    assert (flaky.calls, clock.slept) == (1, [])


def test_the_log_never_contains_the_connection_url(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    secret = "postgresql://aha_app:hunter2@db/aha"

    with caplog.at_level(logging.WARNING, logger="aha.db"):
        retry(Flaky(1, OSError(f"cannot reach {secret}")), clock, wait=60.0)

    assert "hunter2" not in caplog.text


def test_the_pool_and_the_single_connection_both_wait_for_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    async def fake_create_pool(*args: Any, **kwargs: Any) -> FakePool:
        attempts.append("pool")
        if attempts.count("pool") < 3:
            raise ConnectionRefusedError("refused")
        return FakePool(FakeConnection())

    async def fake_connect(*args: Any, **kwargs: Any) -> str:
        attempts.append("conn")
        if attempts.count("conn") < 2:
            raise ConnectionRefusedError("refused")
        return "connection"

    monkeypatch.setattr("aha.db.asyncpg.create_pool", fake_create_pool)
    monkeypatch.setattr("aha.db.asyncpg.connect", fake_connect)
    monkeypatch.setattr("aha.db._INTERVAL", 0.0)  # do not really sleep between attempts

    asyncio.run(Database.connect("postgresql://x/y", max_size=2, statement_timeout=1.0, wait=30.0))
    assert asyncio.run(connect_once("postgresql://x/y", wait=30.0)) == cast("Any", "connection")

    assert attempts == ["pool", "pool", "pool", "conn", "conn"]


def test_without_a_wait_the_first_failure_is_final(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    async def refusing_pool(*args: Any, **kwargs: Any) -> FakePool:
        attempts.append("pool")
        raise ConnectionRefusedError("refused")

    async def refusing_connect(*args: Any, **kwargs: Any) -> str:
        attempts.append("conn")
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr("aha.db.asyncpg.create_pool", refusing_pool)
    monkeypatch.setattr("aha.db.asyncpg.connect", refusing_connect)
    monkeypatch.setattr("aha.db._INTERVAL", 0.01)  # even a very short wait would allow a retry

    with pytest.raises(ConnectionRefusedError):
        asyncio.run(Database.connect("postgresql://x/y", max_size=2, statement_timeout=1.0))
    with pytest.raises(ConnectionRefusedError):
        asyncio.run(connect_once("postgresql://x/y"))

    assert attempts == ["pool", "conn"]  # one attempt each: the default is not to wait
