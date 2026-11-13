"""The SQL behind the job scheduler (see :mod:`aha.scheduler`).

Kept separate from the scheduling logic because it is pure SQL: it is tested against a real
PostgreSQL (``tests/test_postgres_integration.py``), which a string-mutating tool cannot do
meaningfully, so this module is excluded from mutation testing (``do_not_mutate`` in
``pyproject.toml``) while :mod:`aha.scheduler` is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aha.db import Database

__all__ = ["Job", "JobStore"]


@dataclass(frozen=True, slots=True)
class Job:
    """A claimed unit of work."""

    id: int
    kind: str
    run_at: datetime
    user_id: int | None
    guild_id: int | None
    payload: dict[str, Any]
    #: How many times this job has been claimed, including this claim.
    attempts: int


class JobStore:
    """Reads and writes ``scheduled_jobs``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def enqueue(
        self,
        kind: str,
        run_at: datetime,
        *,
        user_id: int | None = None,
        guild_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Add a job; at least one of ``user_id`` and ``guild_id`` is required (privacy)."""
        async with self._db.acquire() as conn:
            job_id = await conn.fetchval(
                "INSERT INTO scheduled_jobs (kind, run_at, user_id, guild_id, payload) "
                "VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id",
                kind,
                run_at,
                user_id,
                guild_id,
                json.dumps(payload or {}),
            )
        return int(job_id)

    async def count(self, kind: str, user_id: int) -> int:
        """How many live (not dead) jobs of ``kind`` a user has."""
        async with self._db.acquire() as conn:
            value = await conn.fetchval(
                "SELECT count(*) FROM scheduled_jobs WHERE kind = $1 AND user_id = $2 AND NOT dead",
                kind,
                user_id,
            )
        return int(value)

    async def list_for_user(self, kind: str, user_id: int, limit: int) -> list[Job]:
        """A user's live jobs of ``kind``, soonest first."""
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, kind, run_at, user_id, guild_id, payload, attempts "
                "FROM scheduled_jobs WHERE kind = $1 AND user_id = $2 AND NOT dead "
                "ORDER BY run_at, id LIMIT $3",
                kind,
                user_id,
                limit,
            )
        return [_job(row) for row in rows]

    async def cancel(self, kind: str, user_id: int, job_id: int) -> bool:
        """Delete one of a user's own jobs; ``False`` when it is not theirs or is gone."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM scheduled_jobs WHERE id = $1 AND kind = $2 AND user_id = $3",
                job_id,
                kind,
                user_id,
            )
        return result.rsplit(" ", 1)[-1] == "1"

    async def claim(self, limit: int, lease_seconds: float) -> list[Job]:
        """Claim up to ``limit`` due jobs for ``lease_seconds``; claimers never overlap."""
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                "UPDATE scheduled_jobs SET locked_until = now() + make_interval(secs => $2), "
                "attempts = attempts + 1 WHERE id IN ("
                "SELECT id FROM scheduled_jobs WHERE NOT dead AND run_at <= now() "
                "AND (locked_until IS NULL OR locked_until < now()) "
                "ORDER BY run_at LIMIT $1 FOR UPDATE SKIP LOCKED) "
                "RETURNING id, kind, run_at, user_id, guild_id, payload, attempts",
                limit,
                lease_seconds,
            )
        return [_job(row) for row in rows]

    async def complete(self, job_id: int) -> None:
        """Delete a finished job."""
        async with self._db.acquire() as conn:
            await conn.execute("DELETE FROM scheduled_jobs WHERE id = $1", job_id)

    async def fail(self, job_id: int, error: str, *, retry_in: float, dead: bool) -> None:
        """Record a failure: retry after ``retry_in`` seconds, or mark the job dead."""
        async with self._db.acquire() as conn:
            await conn.execute(
                "UPDATE scheduled_jobs SET locked_until = NULL, last_error = $2, "
                "run_at = now() + make_interval(secs => $3), dead = $4, "
                "dead_at = CASE WHEN $4 THEN now() ELSE NULL END WHERE id = $1",
                job_id,
                error,
                retry_in,
                dead,
            )

    async def lag(self) -> float:
        """Seconds the oldest due, unclaimed job has been waiting (0 when none)."""
        async with self._db.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COALESCE(EXTRACT(EPOCH FROM now() - min(run_at)), 0) "
                "FROM scheduled_jobs WHERE NOT dead AND run_at <= now() "
                "AND (locked_until IS NULL OR locked_until < now())"
            )
        return float(value)

    async def purge_dead(self, older_than_seconds: float) -> int:
        """Delete dead jobs that died more than ``older_than_seconds`` ago; return how many."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM scheduled_jobs WHERE dead "
                "AND dead_at < now() - make_interval(secs => $1)",
                older_than_seconds,
            )
        return int(result.rsplit(" ", 1)[-1])

    async def purge_user(self, user_id: int) -> int:
        """Delete every job belonging to a user (privacy deletion)."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM scheduled_jobs WHERE user_id = $1", user_id
            )
        return int(result.rsplit(" ", 1)[-1])

    async def purge_guild(self, guild_id: int) -> int:
        """Delete every job belonging to a guild (privacy deletion)."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM scheduled_jobs WHERE guild_id = $1", guild_id
            )
        return int(result.rsplit(" ", 1)[-1])


def _job(row: Any) -> Job:
    """Build a :class:`Job` from a ``scheduled_jobs`` row."""
    return Job(
        id=row["id"],
        kind=row["kind"],
        run_at=row["run_at"],
        user_id=row["user_id"],
        guild_id=row["guild_id"],
        payload=json.loads(row["payload"]),
        attempts=row["attempts"],
    )
