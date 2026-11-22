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
from aha.guildsettings_store import GuildSettings, GuildSettingsStore
from aha.identify import IdentifyCoordinator
from aha.jobstore import JobStore
from aha.showstore import ShowLimitError, ShowStore
from aha.summarize_store import UsageStore

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


@pytest_asyncio.fixture
async def settings_store(store: JobStore) -> GuildSettingsStore:
    """The guild-settings store over the same migrated schema as ``store``."""
    return GuildSettingsStore(store._db)


async def test_guild_settings_default_to_no_row_and_upserts_are_atomic(
    settings_store: GuildSettingsStore,
) -> None:
    assert await settings_store.get(5) is None

    await settings_store.set_log_channel(5, 99, by=7)
    await settings_store.disable(5, "roll", by=8)
    await settings_store.disable(5, "coin", by=8)
    await settings_store.disable(5, "roll", by=9)  # idempotent

    row = await settings_store.get(5)
    assert row == GuildSettings(5, disabled_commands=frozenset({"roll", "coin"}), log_channel_id=99)

    await settings_store.enable(5, "roll", by=9)
    await settings_store.enable(5, "never-disabled", by=9)
    await settings_store.set_log_channel(5, None, by=9)
    row = await settings_store.get(5)
    assert row == GuildSettings(5, disabled_commands=frozenset({"coin"}))
    assert await settings_store.get(6) is None  # guilds are separate


async def test_enabling_in_a_guild_with_no_row_creates_nothing(
    settings_store: GuildSettingsStore,
) -> None:
    await settings_store.enable(5, "roll", by=1)

    assert await settings_store.get(5) is None


async def test_deleting_guild_settings_reports_whether_there_was_a_row(
    settings_store: GuildSettingsStore,
) -> None:
    await settings_store.set_log_channel(5, 1, by=1)

    assert await settings_store.delete(5) is True
    assert await settings_store.delete(5) is False
    assert await settings_store.get(5) is None


async def test_the_disabled_command_cap_is_enforced_by_the_database(
    settings_store: GuildSettingsStore,
) -> None:
    for number in range(100):
        await settings_store.disable(5, f"c{number}", by=1)

    with pytest.raises(asyncpg.CheckViolationError):
        await settings_store.disable(5, "one-too-many", by=1)


async def test_the_identify_lock_serialises_a_bucket_across_connections(
    store: JobStore,
) -> None:
    released: list[asyncio.Task[None]] = []

    def spawn(factory: Any, name: str) -> None:
        released.append(asyncio.create_task(factory(), name=name))

    first = IdentifyCoordinator(store._db, spawn, hold=0.3, poll=0.05, timeout=5.0)
    impatient = IdentifyCoordinator(store._db, spawn, hold=0.3, poll=0.05, timeout=0.1)

    await first.acquire(0)
    await impatient.acquire(0)  # same bucket while the first still holds it: gives up
    assert len(released) == 1

    await impatient.acquire(1)  # another bucket is free at once
    assert len(released) == 2

    await asyncio.gather(*released)  # the holds elapse and the locks are released
    await impatient.acquire(0)
    assert len(released) == 3
    await asyncio.gather(*released)


async def test_forgetme_removes_every_row_belonging_to_the_user(
    store: JobStore, settings_store: GuildSettingsStore
) -> None:
    from aha.privacy import UserPurge, purge_user

    soon = datetime.now(UTC) + timedelta(hours=1)
    await store.enqueue("reminder", soon, user_id=7, guild_id=1)
    await store.enqueue("reminder", soon, user_id=7)
    keep = await store.enqueue("reminder", soon, user_id=8)
    await settings_store.set_log_channel(1, 5, by=7)
    await settings_store.disable(2, "roll", by=7)
    await settings_store.disable(3, "roll", by=8)

    usage = UsageStore(store._db)
    await usage.consume(7, 10)
    await usage.consume(8, 10)

    shows = ShowStore(store._db)
    result = await purge_user(store, settings_store, usage, shows, 7)

    assert result == UserPurge(jobs=2, settings=2, usage=1, shows=0)
    assert await usage.consume(8, 1) is None  # the other user's counter survived
    assert await store.count("reminder", 7) == 0
    assert [job.id for job in await store.list_for_user("reminder", 8, 10)] == [keep]
    async with store._db.acquire() as conn:
        rows = await conn.fetch("SELECT guild_id, updated_by FROM guild_settings ORDER BY guild_id")
    assert [(r["guild_id"], r["updated_by"]) for r in rows] == [(1, 0), (2, 0), (3, 8)]
    # the settings themselves survive; only the person is forgotten
    assert (await settings_store.get(1)) == GuildSettings(1, log_channel_id=5)


async def test_the_daily_quota_is_atomic_and_refundable(store: JobStore) -> None:
    usage = UsageStore(store._db)

    assert [await usage.consume(7, 2) for _ in range(3)] == [1, 2, None]  # the third is refused
    assert await usage.consume(8, 2) == 1  # another user has their own count

    await usage.refund(7)
    assert await usage.consume(7, 2) == 2  # the refunded one can be used again
    await usage.refund(99)  # refunding a user with no row changes nothing


async def test_concurrent_requests_cannot_overspend_the_quota(store: JobStore) -> None:
    usage = UsageStore(store._db)

    results = await asyncio.gather(*(usage.consume(7, 3) for _ in range(10)))

    assert sorted(r for r in results if r is not None) == [1, 2, 3]
    assert results.count(None) == 7


async def test_old_counters_are_dropped_and_a_users_counters_can_be_purged(
    store: JobStore,
) -> None:
    usage = UsageStore(store._db)
    async with store._db.acquire() as conn:
        await conn.execute(
            "INSERT INTO summarize_usage (user_id, day, used) VALUES "
            "(7, (now() AT TIME ZONE 'utc')::date - 30, 5), "
            "(8, (now() AT TIME ZONE 'utc')::date - 30, 5)"
        )

    await usage.consume(7, 10)  # drops this user's old rows on the way

    async with store._db.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, used FROM summarize_usage ORDER BY user_id")
    assert [(r["user_id"], r["used"]) for r in rows] == [(7, 1), (8, 5)]
    assert await usage.purge_user(7) == 1
    assert await usage.purge_user(7) == 0


async def test_the_summaries_switch_defaults_off_and_is_stored_per_guild(
    settings_store: GuildSettingsStore,
) -> None:
    assert await settings_store.get(5) is None
    await settings_store.set_summaries(5, True, by=1)
    assert (await settings_store.get(5)) == GuildSettings(5, summaries_enabled=True)
    await settings_store.set_summaries(5, False, by=1)
    assert (await settings_store.get(5)) == GuildSettings(5)
    assert await settings_store.get(6) is None


# ----------------------------------------------------------------------------- show reminders


def _shows(store: JobStore) -> ShowStore:
    return ShowStore(store._db)


async def _follow(
    shows: ShowStore,
    *,
    user_id: int = 7,
    show_id: int = 100,
    guild_id: int | None = None,
    channel_id: int | None = None,
    name: str = "Severance",
    run_at: datetime | None = None,
    limit: int = 25,
) -> int | None:
    return await shows.add(
        user_id=user_id,
        guild_id=guild_id,
        channel_id=channel_id,
        show_id=show_id,
        show_name=name,
        lead_seconds=3600,
        locale="de",
        run_at=run_at or datetime.now(UTC) + timedelta(days=1),
        payload={"ep": {"id": 1}},
        limit=limit,
    )


async def test_following_a_show_creates_the_subscription_and_its_job_together(
    store: JobStore,
) -> None:
    shows = _shows(store)
    when = datetime.now(UTC) + timedelta(days=1)

    sub_id = await _follow(shows, run_at=when)

    assert sub_id is not None
    sub = await shows.get(sub_id)
    assert sub is not None
    assert (sub.user_id, sub.show_id, sub.show_name, sub.lead_seconds, sub.locale) == (
        7,
        100,
        "Severance",
        3600,
        "de",
    )
    assert (sub.guild_id, sub.channel_id) == (None, None)
    (item,) = await shows.list_for_user(7, 10)
    assert item.subscription == sub
    assert item.run_at == when
    assert item.payload == {"ep": {"id": 1}, "sub": sub_id}
    (job,) = await store.list_for_user("show", 7, 10)
    assert job.id == sub.job_id


async def test_following_the_same_show_twice_changes_nothing(store: JobStore) -> None:
    shows = _shows(store)
    await _follow(shows)

    assert await _follow(shows) is None

    assert await shows.count(7) == 1
    assert await store.count("show", 7) == 1  # no orphaned second job


async def test_a_different_user_may_follow_the_same_show(store: JobStore) -> None:
    shows = _shows(store)

    assert await _follow(shows, user_id=7) is not None
    assert await _follow(shows, user_id=8) is not None
    assert (await shows.count(7), await shows.count(8)) == (1, 1)


async def test_the_list_is_sorted_by_name_ignoring_case_and_limited(store: JobStore) -> None:
    shows = _shows(store)
    await _follow(shows, show_id=1, name="zeta")
    await _follow(shows, show_id=2, name="Alpha")
    await _follow(shows, show_id=3, name="beta")

    names = [item.subscription.show_name for item in await shows.list_for_user(7, 10)]
    assert names == ["Alpha", "beta", "zeta"]
    assert len(await shows.list_for_user(7, 2)) == 2
    assert await shows.list_for_user(99, 10) == []


async def test_a_subscription_without_a_live_job_lists_with_nothing_scheduled(
    store: JobStore,
) -> None:
    shows = _shows(store)
    sub_id = await _follow(shows)
    sub = await shows.get(sub_id or 0)
    assert sub is not None and sub.job_id is not None
    await store.complete(sub.job_id)  # the job is gone (for instance purged after dying)

    (item,) = await shows.list_for_user(7, 10)

    assert (item.run_at, item.payload) == (None, {})


async def test_a_cancelled_or_unknown_subscription_is_none(store: JobStore) -> None:
    assert await _shows(store).get(123456) is None


async def test_unfollowing_deletes_the_subscription_and_its_job(store: JobStore) -> None:
    shows = _shows(store)
    sub_id = await _follow(shows)

    assert await shows.remove(7, sub_id or 0) is True

    assert await shows.get(sub_id or 0) is None
    assert await store.count("show", 7) == 0
    assert await shows.remove(7, sub_id or 0) is False


async def test_unfollowing_works_when_the_job_is_already_gone(store: JobStore) -> None:
    shows = _shows(store)
    sub_id = await _follow(shows)
    sub = await shows.get(sub_id or 0)
    assert sub is not None and sub.job_id is not None
    async with store._db.acquire() as conn:
        await conn.execute("UPDATE show_subscriptions SET job_id = NULL WHERE id = $1", sub_id)

    assert await shows.remove(7, sub_id or 0) is True
    assert await shows.get(sub_id or 0) is None


async def test_nobody_can_unfollow_for_someone_else(store: JobStore) -> None:
    shows = _shows(store)
    sub_id = await _follow(shows, user_id=7)

    assert await shows.remove(8, sub_id or 0) is False

    assert await shows.count(7) == 1
    assert await store.count("show", 7) == 1


async def test_advancing_swaps_the_job_exactly_once(store: JobStore) -> None:
    shows = _shows(store)
    sub_id = await _follow(shows, guild_id=1, channel_id=2)
    first = await shows.get(sub_id or 0)
    assert first is not None and first.job_id is not None
    later = datetime.now(UTC) + timedelta(days=8)

    assert await shows.advance(sub_id or 0, first.job_id, later, {"ep": {"id": 2}}) is True
    # A redelivery of the old job must not start a second chain.
    assert await shows.advance(sub_id or 0, first.job_id, later, {"ep": {"id": 3}}) is False

    moved = await shows.get(sub_id or 0)
    assert moved is not None and moved.job_id not in (None, first.job_id)
    (item,) = await shows.list_for_user(7, 10)
    assert item.run_at == later
    assert item.payload == {"ep": {"id": 2}, "sub": sub_id}
    new_job = [job for job in await store.list_for_user("show", 7, 10) if job.id == moved.job_id]
    assert [(job.user_id, job.guild_id) for job in new_job] == [
        (7, 1)
    ]  # still erasable by user and guild


async def test_advancing_a_cancelled_subscription_does_nothing(store: JobStore) -> None:
    shows = _shows(store)
    sub_id = await _follow(shows)
    sub = await shows.get(sub_id or 0)
    assert sub is not None and sub.job_id is not None
    await shows.remove(7, sub_id or 0)

    assert await shows.advance(sub_id or 0, sub.job_id, datetime.now(UTC), {}) is False

    assert await store.count("show", 7) == 0


async def test_dropping_needs_the_live_job_id(store: JobStore) -> None:
    shows = _shows(store)
    sub_id = await _follow(shows)
    sub = await shows.get(sub_id or 0)
    assert sub is not None and sub.job_id is not None

    assert await shows.drop(sub_id or 0, sub.job_id + 999) is False
    assert await shows.count(7) == 1
    assert await shows.drop(sub_id or 0, sub.job_id) is True
    assert await shows.count(7) == 0


async def test_a_channel_delivery_needs_its_server(store: JobStore) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await _follow(_shows(store), channel_id=5)  # a channel with no guild


async def test_a_lead_beyond_a_week_is_rejected_by_the_database(store: JobStore) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await _shows(store).add(
            user_id=7,
            guild_id=None,
            channel_id=None,
            show_id=1,
            show_name="x",
            lead_seconds=604801,
            locale="en-US",
            run_at=datetime.now(UTC),
            payload={},
            limit=25,
        )


async def test_forgetting_a_user_removes_their_subscriptions_and_jobs_only(
    store: JobStore,
) -> None:
    shows = _shows(store)
    await _follow(shows, user_id=7, show_id=1)
    await _follow(shows, user_id=7, show_id=2)
    await _follow(shows, user_id=8, show_id=1)

    assert await shows.purge_user(7) == 2

    assert (await shows.count(7), await shows.count(8)) == (0, 1)
    assert (await store.count("show", 7), await store.count("show", 8)) == (0, 1)


async def test_a_departed_server_takes_its_channel_subscriptions_with_it(
    store: JobStore,
) -> None:
    shows = _shows(store)
    await _follow(shows, user_id=7, show_id=1, guild_id=1, channel_id=2)
    await _follow(shows, user_id=7, show_id=2, guild_id=3, channel_id=4)
    await _follow(shows, user_id=7, show_id=3)  # a DM subscription belongs to no server

    assert await shows.purge_guild(1) == 1

    assert await shows.count(7) == 2
    assert await store.count("show", 7) == 2


async def test_the_follow_limit_holds_even_when_requests_race(store: JobStore) -> None:
    shows = _shows(store)
    results = await asyncio.gather(
        *(_follow(shows, show_id=n, limit=3) for n in range(10)), return_exceptions=True
    )

    assert sum(isinstance(r, int) for r in results) == 3
    assert sum(isinstance(r, ShowLimitError) for r in results) == 7
    assert await shows.count(7) == 3
    assert await store.count("show", 7) == 3  # the rejected ones left no job behind


async def test_following_the_same_show_at_the_limit_says_already_following(store: JobStore) -> None:
    shows = _shows(store)
    await _follow(shows, show_id=1, limit=1)

    assert await _follow(shows, show_id=1, limit=1) is None
    with pytest.raises(ShowLimitError):
        await _follow(shows, show_id=2, limit=1)


async def test_the_job_purge_leaves_subscription_jobs_but_deletes_orphans(
    store: JobStore,
) -> None:
    shows = _shows(store)
    sub_id = await _follow(shows)
    sub = await shows.get(sub_id or 0)
    assert sub is not None
    await store.enqueue("reminder", datetime.now(UTC), user_id=7)
    orphan = await store.enqueue("show", datetime.now(UTC), user_id=7)  # no subscription owns it

    assert await store.purge_user(7) == 2  # the reminder and the orphan, not the owned job

    assert [job.id for job in await store.list_for_user("show", 7, 10)] == [sub.job_id]
    assert orphan != sub.job_id


async def test_a_purge_does_not_leave_the_replacement_job_of_a_racing_delivery(
    store: JobStore,
) -> None:
    shows = _shows(store)
    sub_id = await _follow(shows)
    sub = await shows.get(sub_id or 0)
    assert sub is not None and sub.job_id is not None
    when = datetime.now(UTC) + timedelta(days=3)

    async with store._db.acquire() as conn, conn.transaction():
        # A delivery is half-way through advancing: it holds the subscription row.
        await conn.fetchrow("SELECT id FROM show_subscriptions WHERE id = $1 FOR UPDATE", sub_id)
        purge = asyncio.create_task(shows.purge_user(7))
        await asyncio.sleep(0.3)  # the purge is now waiting for the row
        new_job = await conn.fetchval(
            "INSERT INTO scheduled_jobs (kind, run_at, user_id, payload) "
            "VALUES ('show', $1, 7, '{}') RETURNING id",
            when,
        )
        await conn.execute(
            "UPDATE show_subscriptions SET job_id = $2 WHERE id = $1", sub_id, new_job
        )
    assert await purge == 1

    # The replacement job is gone. The old one is deleted by the scheduler when the handler
    # returns, so it is still here until then.
    assert [job.id for job in await store.list_for_user("show", 7, 10)] == [sub.job_id]
    await store.complete(sub.job_id)
    assert await store.count("show", 7) == 0
    assert await shows.count(7) == 0
