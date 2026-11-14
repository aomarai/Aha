"""Per-guild settings behind a small per-pod cache (#98).

A pod caches each guild for ``ttl`` seconds. Writes happen on the pod that owns the guild's shard
and also serve its reads (ADR 0001 section 2), and a write drops that pod's entry, so a change takes
effect on the next invocation there; another pod catches up within the TTL. If a write path ever
runs elsewhere (a dashboard), add ``LISTEN/NOTIFY`` rather than a shorter TTL.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Protocol

from discord import app_commands
from discord.ext import commands

from aha.guildsettings_store import GuildSettings

__all__ = [
    "PROTECTED_COMMANDS",
    "CommandDisabled",
    "GuildSettings",
    "GuildSettingsCache",
    "SettingsBackend",
]

#: Commands that can never be disabled, so an admin cannot lock themselves out.
PROTECTED_COMMANDS = frozenset({"config", "configure", "help"})


class CommandDisabled(app_commands.CheckFailure, commands.CheckFailure):
    """The command is disabled in this server (slash and message surfaces)."""


class SettingsBackend(Protocol):
    """What the cache needs from :class:`aha.guildsettings_store.GuildSettingsStore`."""

    async def get(self, guild_id: int) -> GuildSettings | None:
        """The guild's row, or ``None``."""

    async def set_log_channel(self, guild_id: int, channel_id: int | None, by: int) -> None:
        """Set or clear the log channel."""

    async def set_summaries(self, guild_id: int, enabled: bool, by: int) -> None:
        """Turn summaries on or off."""

    async def disable(self, guild_id: int, command: str, by: int) -> None:
        """Disable a command."""

    async def enable(self, guild_id: int, command: str, by: int) -> None:
        """Enable a command."""

    async def delete(self, guild_id: int) -> bool:
        """Delete the row; ``True`` if there was one."""


class GuildSettingsCache:
    """Read-through cache with a TTL; every write invalidates the entry on this pod."""

    def __init__(
        self,
        store: SettingsBackend,
        *,
        ttl: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[int, tuple[float, GuildSettings]] = {}

    async def get(self, guild_id: int) -> GuildSettings:
        """The guild's settings (defaults when it has none), at most ``ttl`` seconds old."""
        cached = self._entries.get(guild_id)
        if cached is not None and self._clock() - cached[0] < self._ttl:
            return cached[1]
        settings = await self._store.get(guild_id) or GuildSettings(guild_id)
        self._entries[guild_id] = (self._clock(), settings)
        return settings

    async def set_log_channel(self, guild_id: int, channel_id: int | None, by: int) -> None:
        await self._store.set_log_channel(guild_id, channel_id, by)
        self._entries.pop(guild_id, None)

    async def set_summaries(self, guild_id: int, enabled: bool, by: int) -> None:
        await self._store.set_summaries(guild_id, enabled, by)
        self._entries.pop(guild_id, None)

    async def disable(self, guild_id: int, command: str, by: int) -> None:
        await self._store.disable(guild_id, command, by)
        self._entries.pop(guild_id, None)

    async def enable(self, guild_id: int, command: str, by: int) -> None:
        await self._store.enable(guild_id, command, by)
        self._entries.pop(guild_id, None)

    async def delete(self, guild_id: int) -> bool:
        """Delete the guild's settings (the guild left or was purged)."""
        deleted = await self._store.delete(guild_id)
        self._entries.pop(guild_id, None)
        return deleted
