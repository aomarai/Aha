"""Unit tests for the database pool wrapper (the real pool is faked)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from aha.db import APPLICATION_NAME, Database, connect_once


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
