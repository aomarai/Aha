"""Unit tests for the migration runner, using a fake connection (no database needed)."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, cast

import pytest

from aha.dbmigrate import (
    LOCK_ID,
    LOCK_TIMEOUT,
    Migration,
    MigrationError,
    SchemaError,
    load_migrations,
    migrate,
    require_current,
    status,
)

A = Migration(1, "baseline", "SELECT 1;")
B = Migration(2, "add_things", "CREATE TABLE things (id int);")
C = Migration(3, "more", "CREATE TABLE more (id int);")


class FakeConnection:
    """Records every statement and models the bookkeeping table and transactions."""

    def __init__(self, applied: list[int] | None = None, fail_on: str | None = None) -> None:
        self.table_exists = applied is not None
        self.applied = list(applied or [])
        self.fail_on = fail_on
        self.log: list[str] = []
        self._pending_rows: list[int] = []

    async def execute(self, query: str, *args: Any) -> str:
        self.log.append(query if not args else f"{query} {args}")
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("boom")
        if query.startswith("CREATE TABLE IF NOT EXISTS schema_migrations"):
            self.table_exists = True
        if query.startswith("INSERT INTO schema_migrations"):
            self._pending_rows.append(args[0])
        return "OK"

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.log.append(query)
        return "schema_migrations" if self.table_exists else None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        self.log.append(query)
        return [{"version": version} for version in sorted(self.applied)]

    def transaction(self) -> AbstractAsyncContextManager[Any]:
        connection = self

        class Transaction:
            async def __aenter__(self) -> None:
                connection.log.append("BEGIN")
                connection._pending_rows = []

            async def __aexit__(self, kind: Any, *rest: object) -> None:
                if kind is None:
                    connection.log.append("COMMIT")
                    connection.applied.extend(connection._pending_rows)
                else:
                    connection.log.append("ROLLBACK")
                connection._pending_rows = []

        return Transaction()


# ------------------------------------------------------------------------------ loading


def _dir(tmp_path: Path, *names: str) -> Path:
    for name in names:
        (tmp_path / name).write_text(f"-- {name}\nSELECT 1;\n", encoding="utf-8")
    return tmp_path


def test_migrations_load_in_version_order_with_their_sql(tmp_path: Path) -> None:
    folder = _dir(tmp_path, "0002_second.sql", "0001_first.sql", "0003_third_one.sql")

    migrations = load_migrations(folder)

    assert [(m.version, m.name) for m in migrations] == [
        (1, "first"),
        (2, "second"),
        (3, "third_one"),
    ]
    assert migrations[0].sql == "-- 0001_first.sql\nSELECT 1;\n"


def test_an_empty_directory_has_no_migrations(tmp_path: Path) -> None:
    assert load_migrations(tmp_path) == []


def test_files_that_are_not_sql_are_ignored(tmp_path: Path) -> None:
    folder = _dir(tmp_path, "0001_first.sql")
    (folder / "README.md").write_text("notes", encoding="utf-8")
    (folder / "__init__.py").write_text("", encoding="utf-8")

    assert [m.version for m in load_migrations(folder)] == [1]


@pytest.mark.parametrize(
    "bad", ["1_short.sql", "0001-dash.sql", "0001_Upper.sql", "0001_.sql", "abcd_x.sql", "0001.sql"]
)
def test_a_sql_file_with_a_malformed_name_is_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(MigrationError) as caught:
        load_migrations(_dir(tmp_path, "0001_first.sql", bad))

    assert str(caught.value) == f"{bad}: expected a name like 0002_add_thing.sql"


def test_a_gap_in_the_versions_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(MigrationError) as caught:
        load_migrations(_dir(tmp_path, "0001_first.sql", "0003_third.sql"))

    assert str(caught.value) == (
        "migration versions must be 1, 2, 3, ...; expected 0002 but found 0003_third.sql"
    )


def test_a_duplicate_version_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match=r"expected 0002 but found 0001_other\.sql"):
        load_migrations(_dir(tmp_path, "0001_first.sql", "0001_other.sql"))


def test_versions_must_start_at_one(tmp_path: Path) -> None:
    with pytest.raises(MigrationError, match=r"expected 0001 but found 0002_second\.sql"):
        load_migrations(_dir(tmp_path, "0002_second.sql"))


def test_the_packaged_migrations_load_and_start_with_the_baseline() -> None:
    migrations = load_migrations()

    assert migrations[0].version == 1
    assert migrations[0].name == "baseline"
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))


# ------------------------------------------------------------------------------- status


def test_status_of_a_fresh_database_is_everything_pending() -> None:
    conn = FakeConnection(applied=None)

    result = asyncio.run(status(conn, [A, B]))

    assert result.applied == ()
    assert result.pending == (A, B)
    assert not any(line.startswith("SELECT version") for line in conn.log)  # no table to read


def test_status_lists_what_is_applied_and_what_is_pending() -> None:
    conn = FakeConnection(applied=[1])

    result = asyncio.run(status(conn, [A, B, C]))

    assert result.applied == (1,)
    assert result.pending == (B, C)


def test_status_reports_a_database_ahead_of_the_code_without_complaint() -> None:
    result = asyncio.run(status(FakeConnection(applied=[1, 2, 3]), [A]))

    assert result.applied == (1, 2, 3)
    assert result.pending == ()


def test_status_changes_nothing() -> None:
    conn = FakeConnection(applied=[1])

    asyncio.run(status(conn, [A, B]))

    assert not any(line.startswith(("INSERT", "CREATE", "SET", "BEGIN")) for line in conn.log)


# ------------------------------------------------------------------------------ migrate


def test_migrate_applies_each_pending_migration_in_its_own_transaction() -> None:
    conn = FakeConnection(applied=None)

    applied = asyncio.run(migrate(conn, [A, B]))

    assert applied == [A, B]
    assert conn.applied == [1, 2]
    assert conn.log == [
        f"SELECT pg_advisory_lock($1) ({LOCK_ID},)",
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version integer PRIMARY KEY, name text NOT NULL, "
        "applied_at timestamptz NOT NULL DEFAULT now())",
        "SELECT to_regclass('schema_migrations')",
        "SELECT version FROM schema_migrations ORDER BY version",
        "BEGIN",
        "SET LOCAL statement_timeout = 0",
        f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'",
        "SELECT 1;",
        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2) (1, 'baseline')",
        "COMMIT",
        "BEGIN",
        "SET LOCAL statement_timeout = 0",
        f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'",
        "CREATE TABLE things (id int);",
        "INSERT INTO schema_migrations (version, name) VALUES ($1, $2) (2, 'add_things')",
        "COMMIT",
        f"SELECT pg_advisory_unlock($1) ({LOCK_ID},)",
    ]


def test_migrate_skips_what_is_already_applied() -> None:
    conn = FakeConnection(applied=[1])

    applied = asyncio.run(migrate(conn, [A, B, C]))

    assert applied == [B, C]
    assert "SELECT 1;" not in conn.log
    assert conn.applied == [1, 2, 3]


def test_migrate_twice_is_a_no_op_the_second_time() -> None:
    conn = FakeConnection(applied=None)

    first = asyncio.run(migrate(conn, [A, B]))
    second = asyncio.run(migrate(conn, [A, B]))

    assert (first, second) == ([A, B], [])
    assert conn.applied == [1, 2]


def test_migrate_with_nothing_to_do_still_locks_and_unlocks() -> None:
    conn = FakeConnection(applied=[1])

    assert asyncio.run(migrate(conn, [A])) == []
    assert conn.log[0] == f"SELECT pg_advisory_lock($1) ({LOCK_ID},)"
    assert conn.log[-1] == f"SELECT pg_advisory_unlock($1) ({LOCK_ID},)"
    assert "BEGIN" not in conn.log


def test_a_failing_migration_rolls_back_stops_the_run_and_still_unlocks() -> None:
    conn = FakeConnection(applied=None, fail_on="CREATE TABLE things")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(migrate(conn, [A, B, C]))

    assert conn.applied == [1]  # the earlier one stays, the failing one is not recorded
    assert "ROLLBACK" in conn.log
    assert "CREATE TABLE more (id int);" not in conn.log  # C never ran
    assert conn.log[-1] == f"SELECT pg_advisory_unlock($1) ({LOCK_ID},)"


def test_the_lock_key_is_stable() -> None:
    assert LOCK_ID == 7_267_000_001
    assert LOCK_TIMEOUT == "5s"


# ------------------------------------------------------------------------- require_current


def test_a_current_schema_passes_the_startup_check() -> None:
    asyncio.run(require_current(FakeConnection(applied=[1, 2]), [A, B]))


def test_a_database_ahead_of_this_build_passes_the_startup_check() -> None:
    asyncio.run(require_current(FakeConnection(applied=[1, 2, 3]), [A, B]))


def test_a_schema_that_is_behind_fails_the_startup_check_naming_what_is_missing() -> None:
    with pytest.raises(SchemaError) as caught:
        asyncio.run(require_current(FakeConnection(applied=[1]), [A, B, C]))

    assert str(caught.value) == (
        "The database schema is behind (pending: 0002, 0003). Run 'aha migrate'."
    )


def test_a_database_with_no_schema_at_all_fails_the_startup_check() -> None:
    with pytest.raises(SchemaError, match=r"pending: 0001\)\. Run 'aha migrate'"):
        asyncio.run(require_current(FakeConnection(applied=None), [A]))


class _Entry:
    """A directory entry whose ``read_text`` insists on the encoding it is asked for."""

    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self._text = text

    def read_text(self, encoding: str | None = None) -> str:
        assert encoding == "utf-8", f"expected utf-8, got {encoding!r}"
        return self._text


class _Dir:
    """Lists its entries in the order given (the real file system promises no order)."""

    def __init__(self, *entries: _Entry) -> None:
        self._entries = entries

    def iterdir(self) -> Any:
        return iter(self._entries)


def test_migrations_are_ordered_by_file_name_whatever_order_the_directory_lists_them() -> None:
    folder = _Dir(_Entry("0003_c.sql", "c"), _Entry("0001_a.sql", "a"), _Entry("0002_b.sql", "b"))

    migrations = load_migrations(cast("Any", folder))

    assert [(m.version, m.sql) for m in migrations] == [(1, "a"), (2, "b"), (3, "c")]


def test_sql_is_read_as_utf8_so_non_ascii_text_survives() -> None:
    folder = _Dir(_Entry("0001_a.sql", "-- caf\u00e9\nSELECT 1;"))

    (migration,) = load_migrations(cast("Any", folder))

    assert migration.sql == "-- caf\u00e9\nSELECT 1;"


def test_a_non_sql_entry_that_sorts_first_does_not_hide_the_migrations() -> None:
    folder = _Dir(
        _Entry("0000_notes.txt", "x"), _Entry("0001_a.sql", "a"), _Entry("0002_b.sql", "b")
    )

    assert [m.version for m in load_migrations(cast("Any", folder))] == [1, 2]
