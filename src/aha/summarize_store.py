"""The SQL behind the ``/summarize`` daily quota (#110).

Counters only, never the text that was summarized. Pure SQL, verified against a real PostgreSQL
(tests/test_postgres_integration.py) and excluded from mutation testing like :mod:`aha.jobstore`.
"""

from __future__ import annotations

from aha.db import Database

__all__ = ["KEEP_DAYS", "UsageStore"]

#: Days of counters kept; yesterday's would already be enough to enforce the quota.
KEEP_DAYS = 7


class UsageStore:
    """Reads and writes ``summarize_usage``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def consume(self, user_id: int, limit: int) -> int | None:
        """Use one of the user's summaries for today (UTC): the new count, or ``None`` at the limit.

        One atomic statement, so two pods cannot both take the last one. Old counters of this user
        are dropped on the way, so the table does not grow without bound.
        """
        async with self._db.acquire() as conn:
            await conn.execute(
                "DELETE FROM summarize_usage WHERE user_id = $1 "
                "AND day < (now() AT TIME ZONE 'utc')::date - $2::integer",
                user_id,
                KEEP_DAYS,
            )
            used = await conn.fetchval(
                "INSERT INTO summarize_usage (user_id, day, used) "
                "VALUES ($1, (now() AT TIME ZONE 'utc')::date, 1) "
                "ON CONFLICT (user_id, day) DO UPDATE SET used = summarize_usage.used + 1 "
                "WHERE summarize_usage.used < $2 RETURNING used",
                user_id,
                limit,
            )
        return None if used is None else int(used)

    async def refund(self, user_id: int) -> None:
        """Give back today's summary when the request failed through no fault of the user."""
        async with self._db.acquire() as conn:
            await conn.execute(
                "UPDATE summarize_usage SET used = used - 1 "
                "WHERE user_id = $1 AND day = (now() AT TIME ZONE 'utc')::date AND used > 0",
                user_id,
            )

    async def purge_user(self, user_id: int) -> int:
        """Delete every counter of a user (privacy deletion)."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM summarize_usage WHERE user_id = $1", user_id
            )
        return int(result.rsplit(" ", 1)[-1])
