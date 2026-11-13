"""Integration tests against a real PostgreSQL; skipped unless AHA_TEST_DATABASE_URL is set.

They prove what a fake connection cannot: that a failed migration really rolls back, that the
advisory lock serialises concurrent runs, that the statement timeout cancels slow queries, and
that ``lock_timeout`` stops a migration from queueing behind another transaction's lock.
Each test works in its own schema, so they never touch real data. This is the one module that
uses pytest-asyncio; the rest of the suite drives coroutines with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from aha import dbmigrate
from aha.db import Database
from aha.dbmigrate import (
    Migration,
    SchemaError,
    load_migrations,
    migrate,
    require_current,
    status,
)

URL = os.environ.get("AHA_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(URL is None, reason="AHA_TEST_DATABASE_URL is not set"),
    pytest.mark.asyncio,
]

A = Migration(1, "one", "CREATE TABLE t1 (id int);")
B = Migration(2, "two", "CREATE TABLE t2 (id int);")
BAD = Migration(2, "bad", "CREATE TABLE t2 (id int); SELECT 1/0;")
C = Migration(3, "three", "CREATE TABLE t3 (id int);")


@pytest_asyncio.fixture
async def schema() -> AsyncIterator[str]:
    assert URL is not None
    name = f"t_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(URL)
    await admin.execute(f'CREATE SCHEMA "{name}"')
    try:
        yield name
    finally:
        await admin.execute(f'DROP SCHEMA "{name}" CASCADE')
        await admin.close()


async def _connect(schema: str) -> asyncpg.Connection[Any]:
    assert URL is not None
    return await asyncpg.connect(URL, server_settings={"search_path": schema})


async def _tables(conn: asyncpg.Connection[Any], schema: str) -> set[str]:
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = $1", schema)
    return {row["tablename"] for row in rows}


async def test_packaged_migrations_apply_and_a_second_run_is_a_no_op(schema: str) -> None:
    conn = await _connect(schema)
    try:
        migrations = load_migrations()
        first = await migrate(conn, migrations)
        second = await migrate(conn, migrations)

        assert len(first) == len(migrations)
        assert second == []
        assert (await status(conn, migrations)).pending == ()
    finally:
        await conn.close()


async def test_migrations_create_their_tables_and_record_versions(schema: str) -> None:
    conn = await _connect(schema)
    try:
        await migrate(conn, [A, B])

        assert await _tables(conn, schema) == {"schema_migrations", "t1", "t2"}
        rows = await conn.fetch("SELECT version, name FROM schema_migrations ORDER BY version")
        assert [(r["version"], r["name"]) for r in rows] == [(1, "one"), (2, "two")]
    finally:
        await conn.close()


async def test_a_failing_migration_rolls_back_and_stops_the_run(schema: str) -> None:
    conn = await _connect(schema)
    try:
        with pytest.raises(asyncpg.DivisionByZeroError):
            await migrate(conn, [A, BAD, C])

        assert await _tables(conn, schema) == {"schema_migrations", "t1"}  # t2 rolled back, no t3
        rows = await conn.fetch("SELECT version FROM schema_migrations")
        assert [r["version"] for r in rows] == [1]
        # The advisory lock was released: this same session can take it again.
        assert await conn.fetchval("SELECT pg_try_advisory_lock($1)", dbmigrate.LOCK_ID) is True
    finally:
        await conn.close()


async def test_concurrent_runs_apply_each_migration_once(schema: str) -> None:
    slow = Migration(1, "slow", "SELECT pg_sleep(0.6); CREATE TABLE slow_t (id int);")
    first, second = await _connect(schema), await _connect(schema)
    try:
        results = await asyncio.gather(migrate(first, [slow]), migrate(second, [slow]))

        assert sorted(len(r) for r in results) == [0, 1]  # one applied it, one found it done
        assert await first.fetchval("SELECT count(*) FROM schema_migrations") == 1
    finally:
        await first.close()
        await second.close()


async def test_a_migration_does_not_queue_behind_another_transactions_lock(
    schema: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dbmigrate, "LOCK_TIMEOUT", "200ms")
    holder, runner = await _connect(schema), await _connect(schema)
    try:
        await migrate(runner, [A])
        transaction = holder.transaction()
        await transaction.start()
        await holder.execute("LOCK TABLE t1 IN ACCESS EXCLUSIVE MODE")
        alter = Migration(2, "alter", "ALTER TABLE t1 ADD COLUMN x int;")

        with pytest.raises(asyncpg.LockNotAvailableError):
            await asyncio.wait_for(migrate(runner, [A, alter]), timeout=5)

        await transaction.rollback()
    finally:
        await holder.close()
        await runner.close()


async def test_require_current_tracks_a_real_database(schema: str) -> None:
    conn = await _connect(schema)
    try:
        with pytest.raises(SchemaError, match=r"pending: 0001, 0002"):
            await require_current(conn, [A, B])
        await migrate(conn, [A, B])
        await require_current(conn, [A, B])
        await require_current(conn, [A])  # a database ahead of the code is fine
    finally:
        await conn.close()


async def test_the_pool_cancels_a_query_that_exceeds_the_statement_timeout() -> None:
    assert URL is not None
    database = await Database.connect(URL, max_size=2, statement_timeout=0.2)
    try:
        async with database.acquire() as conn:
            assert await conn.fetchval("SHOW statement_timeout") == "200ms"
            assert await conn.fetchval("SHOW application_name") == "aha"
            with pytest.raises(asyncpg.QueryCanceledError):
                await conn.fetchval("SELECT pg_sleep(2)")
        assert await database.ping() is True
    finally:
        await database.close()
