"""Plain-SQL schema migrations for PostgreSQL.

Why not an ORM or Alembic: the schema is small, the SQL is the reviewable artefact, and the rules
that matter are about *rollout*, not tooling:

* **Run as a Job, never at pod startup.** N pods starting together would race; ``aha migrate`` is
  one process behind an advisory lock (so even two Jobs cannot overlap).
* **Forward-only, expand/contract.** A release only adds (nullable columns, new tables); anything
  destructive ships a release later, after every old pod has drained. The previous release must
  keep working against the new schema, so a rollback never needs a down-migration. A database that
  is *ahead* of the running code is therefore normal and is not an error.
* **Bounded locking.** Each migration runs in one transaction with a short ``lock_timeout`` so an
  ``ALTER TABLE`` waiting behind a long transaction fails loudly instead of queueing and blocking
  every query behind it.
"""

from __future__ import annotations

import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, Protocol

__all__ = [
    "LOCK_ID",
    "Connection",
    "Migration",
    "MigrationError",
    "MigrationStatus",
    "SchemaError",
    "load_migrations",
    "migrate",
    "require_current",
    "status",
]

#: Key of the advisory lock that serialises migration runs (the digits spell "aha-m").
LOCK_ID = 7_267_000_001

#: How long a migration waits for a lock another transaction holds before failing.
LOCK_TIMEOUT = "5s"

_FILENAME = re.compile(r"^(\d{4})_([a-z0-9][a-z0-9_]*)\.sql$")

_CREATE_TABLE = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version integer PRIMARY KEY, "
    "name text NOT NULL, "
    "applied_at timestamptz NOT NULL DEFAULT now())"
)


class MigrationError(Exception):
    """The migration files are malformed (a bad name, a gap, a duplicate)."""


class SchemaError(Exception):
    """The database is behind the migrations this build knows about."""


class Connection(Protocol):
    """The slice of ``asyncpg.Connection`` this module uses (so tests can fake it)."""

    async def execute(self, query: str, *args: Any) -> str:
        """Run a statement."""

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        """Run a query and return all its rows."""

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Run a query and return the first column of the first row."""

    def transaction(self) -> AbstractAsyncContextManager[Any]:
        """Return an async context manager that wraps a transaction."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One migration file."""

    version: int
    name: str
    sql: str


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    """Which migrations the database has, and which it still needs."""

    applied: tuple[int, ...]
    pending: tuple[Migration, ...]


def load_migrations(directory: Traversable | None = None) -> list[Migration]:
    """Read ``NNNN_name.sql`` files, in order, from ``directory`` (default: the package's own).

    Raises:
        MigrationError: for a ``.sql`` file with a malformed name, a duplicate version, or a gap
            (versions must run 1, 2, 3, ...: a gap is almost always a missing file).
    """
    folder = directory if directory is not None else Path(__file__).parent / "migrations"
    found: list[Migration] = []
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".sql"):
            continue
        match = _FILENAME.fullmatch(entry.name)
        if match is None:
            raise MigrationError(f"{entry.name}: expected a name like 0002_add_thing.sql")
        found.append(Migration(int(match[1]), match[2], entry.read_text(encoding="utf-8")))
    for expected, migration in enumerate(found, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"migration versions must be 1, 2, 3, ...; expected {expected:04d} "
                f"but found {migration.version:04d}_{migration.name}.sql"
            )
    return found


async def _applied(conn: Connection) -> tuple[int, ...]:
    """Versions already recorded; none if the bookkeeping table does not exist yet."""
    if await conn.fetchval("SELECT to_regclass('schema_migrations')") is None:
        return ()
    rows = await conn.fetch("SELECT version FROM schema_migrations ORDER BY version")
    return tuple(int(row["version"]) for row in rows)


async def status(conn: Connection, migrations: list[Migration]) -> MigrationStatus:
    """Report what is applied and what is pending, without changing anything."""
    applied = await _applied(conn)
    pending = tuple(m for m in migrations if m.version not in applied)
    return MigrationStatus(applied, pending)


async def migrate(conn: Connection, migrations: list[Migration]) -> list[Migration]:
    """Apply every pending migration, in order, and return the ones applied.

    Takes a session-level advisory lock so concurrent runs queue rather than race, and applies each
    migration (and its bookkeeping row) in one transaction: a failure rolls that migration back and
    stops the run, leaving earlier ones in place.
    """
    await conn.execute("SELECT pg_advisory_lock($1)", LOCK_ID)
    try:
        await conn.execute(_CREATE_TABLE)
        applied = await _applied(conn)
        done: list[Migration] = []
        for migration in migrations:
            if migration.version in applied:
                continue
            async with conn.transaction():
                # A migration may legitimately run long, so no statement timeout; but it must
                # not queue behind another transaction's lock and block everything behind it.
                await conn.execute("SET LOCAL statement_timeout = 0")
                await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
                await conn.execute(migration.sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES ($1, $2)",
                    migration.version,
                    migration.name,
                )
            done.append(migration)
        return done
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_ID)


async def require_current(conn: Connection, migrations: list[Migration]) -> None:
    """Raise :class:`SchemaError` unless every migration this build knows has been applied.

    A database *ahead* of this build is fine (a rollback runs old code on a newer schema).
    """
    pending = (await status(conn, migrations)).pending
    if pending:
        versions = ", ".join(f"{m.version:04d}" for m in pending)
        raise SchemaError(
            f"The database schema is behind (pending: {versions}). Run 'aha migrate'."
        )
