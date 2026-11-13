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
from datetime import UTC, datetime, timedelta
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
from aha.jobstore import JobStore

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


# ------------------------------------------------------------------------------ job store


@pytest_asyncio.fixture
async def store(schema: str) -> AsyncIterator[JobStore]:
    """A job store on a pool whose search_path is the test schema, with every migration applied."""
    assert URL is not None
    conn = await _connect(schema)
    try:
        await migrate(conn, load_migrations())
    finally:
        await conn.close()
    pool = await asyncpg.create_pool(
        URL, min_size=1, max_size=4, server_settings={"search_path": schema}
    )
    try:
        yield JobStore(Database(pool))
    finally:
        await pool.close()


def _later(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def test_a_due_job_is_claimed_with_its_data_and_a_future_one_is_not(store: JobStore) -> None:
    due = await store.enqueue(
        "reminder", _later(-1), user_id=33, guild_id=11, payload={"text": "hi", "n": [1, 2]}
    )
    await store.enqueue("reminder", _later(3600), user_id=33)

    claimed = await store.claim(10, 60.0)

    assert [job.id for job in claimed] == [due]
    job = claimed[0]
    assert (job.kind, job.user_id, job.guild_id, job.attempts) == ("reminder", 33, 11, 1)
    assert job.payload == {"text": "hi", "n": [1, 2]}


async def test_a_claimed_job_is_leased_until_the_lease_expires(store: JobStore) -> None:
    await store.enqueue("reminder", _later(-1), user_id=1)

    first = await store.claim(10, 0.3)
    during = await store.claim(10, 0.3)
    await asyncio.sleep(0.5)
    after = await store.claim(10, 60.0)

    assert (len(first), len(during), len(after)) == (1, 0, 1)
    assert after[0].attempts == 2  # attempts are counted at claim, so a crash loop ends


async def test_concurrent_claimers_never_get_the_same_job(store: JobStore) -> None:
    for _ in range(30):
        await store.enqueue("reminder", _later(-1), user_id=1)

    batches = await asyncio.gather(*(store.claim(10, 60.0) for _ in range(4)))
    ids = [job.id for batch in batches for job in batch]

    assert len(ids) == 30
    assert len(set(ids)) == 30  # SKIP LOCKED: no job was handed to two claimers


async def test_jobs_are_claimed_oldest_first_up_to_the_limit(store: JobStore) -> None:
    newest = await store.enqueue("k", _later(-1), user_id=1)
    oldest = await store.enqueue("k", _later(-100), user_id=1)
    middle = await store.enqueue("k", _later(-50), user_id=1)

    claimed = await store.claim(2, 60.0)

    assert [job.id for job in claimed] == [oldest, middle]
    assert newest not in [job.id for job in claimed]


async def test_completing_a_job_deletes_it(store: JobStore) -> None:
    job_id = await store.enqueue("k", _later(-1), user_id=1)
    await store.claim(10, 60.0)

    await store.complete(job_id)

    assert await store.claim(10, 60.0) == []
    assert await store.lag() == 0.0


async def test_a_failed_job_is_retried_later_with_its_error_kept(store: JobStore) -> None:
    job_id = await store.enqueue("k", _later(-1), user_id=1)
    await store.claim(10, 60.0)

    await store.fail(job_id, "ValueError: boom", retry_in=0.2, dead=False)

    assert await store.claim(10, 60.0) == []  # not due yet
    await asyncio.sleep(0.4)
    (retry,) = await store.claim(10, 60.0)
    assert (retry.id, retry.attempts) == (job_id, 2)


async def test_a_dead_job_is_never_claimed_again_and_is_purged_after_retention(
    store: JobStore,
) -> None:
    job_id = await store.enqueue("k", _later(-1), user_id=1)
    await store.claim(10, 60.0)

    await store.fail(job_id, "gave up", retry_in=0.0, dead=True)

    assert await store.claim(10, 60.0) == []
    assert await store.purge_dead(3600.0) == 0  # too recent to delete
    await asyncio.sleep(0.2)
    assert await store.purge_dead(0.1) == 1  # older than 0.1 s


async def test_lag_is_how_long_the_oldest_due_unclaimed_job_has_waited(store: JobStore) -> None:
    assert await store.lag() == 0.0
    await store.enqueue("k", _later(-30), user_id=1)
    await store.enqueue("k", _later(-5), user_id=1)
    await store.enqueue("k", _later(3600), user_id=1)  # not due: ignored

    assert 29.0 < await store.lag() < 40.0

    await store.claim(10, 60.0)  # claimed jobs are being handled, so they are not lag

    assert await store.lag() == 0.0


async def test_a_users_or_a_guilds_jobs_can_be_deleted(store: JobStore) -> None:
    await store.enqueue("k", _later(60), user_id=1, guild_id=10)
    await store.enqueue("k", _later(60), user_id=1, guild_id=11)
    await store.enqueue("k", _later(60), user_id=2, guild_id=10)
    await store.enqueue("k", _later(60), user_id=3)

    assert await store.purge_user(1) == 2
    assert await store.purge_guild(10) == 1  # user 2's job in guild 10
    assert await store.purge_guild(10) == 0
    assert await store.purge_user(3) == 1


async def test_a_job_with_no_owner_is_rejected_by_the_database(store: JobStore) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await store.enqueue("k", _later(1))


async def test_an_empty_kind_is_rejected_by_the_database(store: JobStore) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await store.enqueue("", _later(1), user_id=1)


async def test_user_queries_count_list_and_cancel_only_the_owners_live_jobs(
    store: JobStore,
) -> None:
    soon = datetime.now(UTC) + timedelta(minutes=5)
    later = soon + timedelta(hours=1)
    second = await store.enqueue("reminder", later, user_id=1, payload={"text": "b"})
    first = await store.enqueue("reminder", soon, user_id=1, payload={"text": "a"})
    other_user = await store.enqueue("reminder", soon, user_id=2)
    await store.enqueue("other", soon, user_id=1)

    assert await store.count("reminder", 1) == 2
    assert await store.count("reminder", 3) == 0
    listed = await store.list_for_user("reminder", 1, 10)
    assert [(j.id, j.payload["text"]) for j in listed] == [(first, "a"), (second, "b")]
    assert [j.id for j in await store.list_for_user("reminder", 1, 1)] == [first]

    assert await store.cancel("reminder", 1, other_user) is False  # someone else's
    assert await store.cancel("reminder", 1, 999_999) is False
    assert await store.cancel("reminder", 1, first) is True
    assert await store.cancel("reminder", 1, first) is False
    assert await store.count("reminder", 1) == 1
    assert await store.count("reminder", 2) == 1
