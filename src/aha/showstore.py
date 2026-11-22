"""The SQL behind ``/watch``: show subscriptions and the one scheduled job each of them owns.

A subscription points at its live ``scheduled_jobs`` row through ``job_id``. Moving a subscription
on to its next episode (:meth:`ShowStore.advance`) is a compare-and-swap on that id inside one
transaction, so a job that is delivered twice (a crash after Discord accepted the message, a lease
that expired) cannot start a second chain of reminders. Pure SQL, verified against a real
PostgreSQL (tests/test_postgres_integration.py) and excluded from mutation testing like
:mod:`aha.jobstore`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aha.db import Database

__all__ = ["KIND", "Followed", "ShowStore", "Subscription"]

#: ``scheduled_jobs.kind`` of a subscription's next notification.
KIND = "show"


@dataclass(frozen=True, slots=True)
class Subscription:
    """One user following one show."""

    id: int
    user_id: int
    guild_id: int | None
    #: ``None`` means a direct message.
    channel_id: int | None
    show_id: int
    show_name: str
    lead_seconds: int
    locale: str
    #: The live ``scheduled_jobs`` row, or ``None`` when it died or was lost.
    job_id: int | None


@dataclass(frozen=True, slots=True)
class Followed:
    """A subscription with what is scheduled next for it."""

    subscription: Subscription
    #: When the next notification runs, or ``None`` when nothing is scheduled.
    run_at: datetime | None
    #: The scheduled job's payload (the next episode, if one is known).
    payload: dict[str, Any]


_COLUMNS = (
    "s.id, s.user_id, s.guild_id, s.channel_id, s.show_id, s.show_name, s.lead_seconds, "
    "s.locale, s.job_id"
)


def _subscription(row: Any) -> Subscription:
    return Subscription(
        id=row["id"],
        user_id=row["user_id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        show_id=row["show_id"],
        show_name=row["show_name"],
        lead_seconds=row["lead_seconds"],
        locale=row["locale"],
        job_id=row["job_id"],
    )


async def _enqueue(
    conn: Any,
    sub_id: int,
    user_id: int,
    guild_id: int | None,
    run_at: datetime,
    payload: dict[str, Any],
) -> int:
    """Insert the subscription's job (inside the caller's transaction) and return its id."""
    return int(
        await conn.fetchval(
            "INSERT INTO scheduled_jobs (kind, run_at, user_id, guild_id, payload) "
            "VALUES ($1, $2, $3, $4, $5::jsonb) RETURNING id",
            KIND,
            run_at,
            user_id,
            guild_id,
            json.dumps({**payload, "sub": sub_id}),
        )
    )


class ShowStore:
    """Reads and writes ``show_subscriptions`` (and the jobs they own)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def add(
        self,
        *,
        user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        show_id: int,
        show_name: str,
        lead_seconds: int,
        locale: str,
        run_at: datetime,
        payload: dict[str, Any],
    ) -> int | None:
        """Subscribe and schedule the first notification; ``None`` if already subscribed."""
        async with self._db.acquire() as conn, conn.transaction():
            sub_id = await conn.fetchval(
                "INSERT INTO show_subscriptions "
                "(user_id, guild_id, channel_id, show_id, show_name, lead_seconds, locale) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                "ON CONFLICT (user_id, show_id) DO NOTHING RETURNING id",
                user_id,
                guild_id,
                channel_id,
                show_id,
                show_name,
                lead_seconds,
                locale,
            )
            if sub_id is None:
                return None
            job_id = await _enqueue(conn, sub_id, user_id, guild_id, run_at, payload)
            await conn.execute(
                "UPDATE show_subscriptions SET job_id = $2 WHERE id = $1", sub_id, job_id
            )
        return int(sub_id)

    async def count(self, user_id: int) -> int:
        """How many shows a user follows."""
        async with self._db.acquire() as conn:
            value = await conn.fetchval(
                "SELECT count(*) FROM show_subscriptions WHERE user_id = $1", user_id
            )
        return int(value)

    async def get(self, sub_id: int) -> Subscription | None:
        """One subscription, or ``None`` when it was cancelled."""
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT {_COLUMNS} FROM show_subscriptions s WHERE s.id = $1", sub_id
            )
        return None if row is None else _subscription(row)

    async def list_for_user(self, user_id: int, limit: int) -> list[Followed]:
        """A user's subscriptions, by show name, with what is scheduled for each."""
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS}, j.run_at AS next_run, j.payload AS next_payload "
                "FROM show_subscriptions s LEFT JOIN scheduled_jobs j ON j.id = s.job_id "
                "WHERE s.user_id = $1 ORDER BY lower(s.show_name), s.id LIMIT $2",
                user_id,
                limit,
            )
        return [
            Followed(
                _subscription(row),
                row["next_run"],
                json.loads(row["next_payload"]) if row["next_payload"] else {},
            )
            for row in rows
        ]

    async def remove(self, user_id: int, sub_id: int) -> bool:
        """Unsubscribe one of a user's own shows (and cancel its job); ``False`` if not theirs."""
        async with self._db.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "DELETE FROM show_subscriptions WHERE id = $1 AND user_id = $2 RETURNING job_id",
                sub_id,
                user_id,
            )
            if row is None:
                return False
            if row["job_id"] is not None:
                await conn.execute("DELETE FROM scheduled_jobs WHERE id = $1", row["job_id"])
        return True

    async def advance(
        self, sub_id: int, expected_job_id: int, run_at: datetime, payload: dict[str, Any]
    ) -> bool:
        """Schedule the next notification, but only if ``expected_job_id`` is still the live one.

        ``False`` means the subscription was cancelled or already moved on: do nothing more.
        """
        async with self._db.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT user_id, guild_id FROM show_subscriptions "
                "WHERE id = $1 AND job_id = $2 FOR UPDATE",
                sub_id,
                expected_job_id,
            )
            if row is None:
                return False
            job_id = await _enqueue(conn, sub_id, row["user_id"], row["guild_id"], run_at, payload)
            await conn.execute(
                "UPDATE show_subscriptions SET job_id = $2 WHERE id = $1", sub_id, job_id
            )
        return True

    async def drop(self, sub_id: int, expected_job_id: int) -> bool:
        """Delete a subscription whose show has ended, if ``expected_job_id`` is still its job."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM show_subscriptions WHERE id = $1 AND job_id = $2",
                sub_id,
                expected_job_id,
            )
        return result.rsplit(" ", 1)[-1] == "1"

    async def purge_user(self, user_id: int) -> int:
        """Delete every subscription of a user and their jobs (privacy deletion)."""
        return await self._purge("user_id", user_id)

    async def purge_guild(self, guild_id: int) -> int:
        """Delete every subscription delivered in a guild and their jobs (privacy deletion)."""
        return await self._purge("guild_id", guild_id)

    async def _purge(self, column: str, value: int) -> int:
        # ``column`` is one of two literals above, never user input.
        async with self._db.acquire() as conn, conn.transaction():
            await conn.execute(
                "DELETE FROM scheduled_jobs WHERE id IN "
                f"(SELECT job_id FROM show_subscriptions WHERE {column} = $1)",
                value,
            )
            result: str = await conn.execute(
                f"DELETE FROM show_subscriptions WHERE {column} = $1", value
            )
        return int(result.rsplit(" ", 1)[-1])
