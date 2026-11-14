"""The SQL behind per-guild settings (#98): atomic upserts, no read-modify-write races.

Pure SQL, verified against a real PostgreSQL (tests/test_postgres_integration.py) and excluded from
mutation testing for the same reason as :mod:`aha.jobstore`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aha.db import Database

__all__ = ["GuildSettings", "GuildSettingsStore"]


@dataclass(frozen=True, slots=True)
class GuildSettings:
    """A guild's settings. The defaults apply to a guild with no row."""

    guild_id: int
    locale: str | None = None
    disabled_commands: frozenset[str] = field(default_factory=frozenset)
    log_channel_id: int | None = None


class GuildSettingsStore:
    """Reads and writes ``guild_settings``."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, guild_id: int) -> GuildSettings | None:
        """The guild's row, or ``None`` when it has never changed a setting."""
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT locale, disabled_commands, log_channel_id FROM guild_settings "
                "WHERE guild_id = $1",
                guild_id,
            )
        if row is None:
            return None
        return GuildSettings(
            guild_id,
            locale=row["locale"],
            disabled_commands=frozenset(row["disabled_commands"]),
            log_channel_id=row["log_channel_id"],
        )

    async def set_log_channel(self, guild_id: int, channel_id: int | None, by: int) -> None:
        """Set (or clear with ``None``) the audit log channel."""
        async with self._db.acquire() as conn:
            await conn.execute(
                "INSERT INTO guild_settings (guild_id, log_channel_id, updated_by) "
                "VALUES ($1, $2, $3) ON CONFLICT (guild_id) DO UPDATE "
                "SET log_channel_id = $2, updated_by = $3, updated_at = now()",
                guild_id,
                channel_id,
                by,
            )

    async def disable(self, guild_id: int, command: str, by: int) -> None:
        """Add ``command`` to the disabled set (idempotent)."""
        async with self._db.acquire() as conn:
            await conn.execute(
                "INSERT INTO guild_settings (guild_id, disabled_commands, updated_by) "
                "VALUES ($1, ARRAY[$2]::text[], $3) ON CONFLICT (guild_id) DO UPDATE "
                "SET disabled_commands = array_append("
                "array_remove(guild_settings.disabled_commands, $2), $2), "
                "updated_by = $3, updated_at = now()",
                guild_id,
                command,
                by,
            )

    async def enable(self, guild_id: int, command: str, by: int) -> None:
        """Remove ``command`` from the disabled set (idempotent)."""
        async with self._db.acquire() as conn:
            await conn.execute(
                "UPDATE guild_settings SET "
                "disabled_commands = array_remove(disabled_commands, $2), "
                "updated_by = $3, updated_at = now() WHERE guild_id = $1",
                guild_id,
                command,
                by,
            )

    async def anonymise_user(self, user_id: int) -> int:
        """Replace a user's id as "last changed by" with 0; return how many rows were touched."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "UPDATE guild_settings SET updated_by = 0 WHERE updated_by = $1", user_id
            )
        return int(result.rsplit(" ", 1)[-1])

    async def delete(self, guild_id: int) -> bool:
        """Delete a guild's row; ``True`` when there was one."""
        async with self._db.acquire() as conn:
            result: str = await conn.execute(
                "DELETE FROM guild_settings WHERE guild_id = $1", guild_id
            )
        return result.rsplit(" ", 1)[-1] == "1"
