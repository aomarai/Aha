"""The SQL behind the voice queue (#171): which tracks wait in each guild, and who asked for them.

The lowest id of a guild is the track playing now; finishing it deletes its row, so the next lowest
becomes the head. Adding takes a per-guild lock inside the transaction, so concurrent ``/play``
calls cannot all slip under the queue limit. Pure SQL, verified against a real PostgreSQL
(tests/test_postgres_integration.py) and excluded from mutation testing like :mod:`aha.jobstore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aha.db import Database

__all__ = ["QueueEntry", "QueueFullError", "QueueStore"]


class QueueFullError(Exception):
    """The guild's queue already holds as many tracks as it may."""


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """One queued track."""

    id: int
    guild_id: int
    video_id: str
    title: str
    length_ms: int
    requester_id: int


_COLUMNS = "id, guild_id, video_id, title, length_ms, requester_id"


def _entry(row: Any) -> QueueEntry:
    return QueueEntry(
        id=row["id"],
        guild_id=row["guild_id"],
        video_id=row["video_id"],
        title=row["title"],
        length_ms=row["length_ms"],
        requester_id=row["requester_id"],
    )


class QueueStore:
    """Reads and writes ``voice_queue``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def add(
        self,
        guild_id: int,
        *,
        video_id: str,
        title: str,
        length_ms: int,
        requester_id: int,
        limit: int,
    ) -> QueueEntry:
        """Append a track. Raises :class:`QueueFullError` when the guild already has ``limit``."""
        async with self._db.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"voice-queue:{guild_id}"
            )
            queued = await conn.fetchval(
                "SELECT count(*) FROM voice_queue WHERE guild_id = $1", guild_id
            )
            if queued >= limit:
                raise QueueFullError(f"{limit} tracks")
            row = await conn.fetchrow(
                "INSERT INTO voice_queue (guild_id, video_id, title, length_ms, requester_id) "
                f"VALUES ($1, $2, $3, $4, $5) RETURNING {_COLUMNS}",
                guild_id,
                video_id,
                title,
                length_ms,
                requester_id,
            )
        return _entry(row)

    async def entries(self, guild_id: int, limit: int) -> list[QueueEntry]:
        """The guild's tracks in play order (the first is the one playing now)."""
        async with self._db.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT {_COLUMNS} FROM voice_queue WHERE guild_id = $1 ORDER BY id LIMIT $2",
                guild_id,
                limit,
            )
        return [_entry(row) for row in rows]

    async def head(self, guild_id: int) -> QueueEntry | None:
        """The track at the front, or ``None`` for an empty queue."""
        found = await self.entries(guild_id, 1)
        return found[0] if found else None

    async def count(self, guild_id: int) -> int:
        """How many tracks the guild has queued, the playing one included."""
        async with self._db.acquire() as conn:
            value = await conn.fetchval(
                "SELECT count(*) FROM voice_queue WHERE guild_id = $1", guild_id
            )
        return int(value)

    async def finish(self, guild_id: int, entry_id: int) -> bool:
        """Delete a finished track; ``False`` if it was already gone (so a repeat is harmless)."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM voice_queue WHERE guild_id = $1 AND id = $2", guild_id, entry_id
            )
        return result.rsplit(" ", 1)[-1] == "1"

    async def remove(self, guild_id: int, entry_id: int) -> QueueEntry | None:
        """Delete one queued track and return it; ``None`` if it is not there (any more).

        By id, not position, so a permission check made on the entry the user saw cannot end up
        deleting a different one after the queue moved.
        """
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                f"DELETE FROM voice_queue WHERE guild_id = $1 AND id = $2 RETURNING {_COLUMNS}",
                guild_id,
                entry_id,
            )
        return None if row is None else _entry(row)

    async def clear(self, guild_id: int) -> int:
        """Delete every queued track of a guild; return how many."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM voice_queue WHERE guild_id = $1", guild_id
            )
        return int(result.rsplit(" ", 1)[-1])

    async def purge_user(self, user_id: int) -> int:
        """Delete every track a user queued (privacy deletion)."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM voice_queue WHERE requester_id = $1", user_id
            )
        return int(result.rsplit(" ", 1)[-1])

    async def purge_guild(self, guild_id: int) -> int:
        """Delete a departed guild's queue (privacy deletion)."""
        return await self.clear(guild_id)
