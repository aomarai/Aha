"""Tests for the per-guild settings cache."""

from __future__ import annotations

import asyncio

from discord import app_commands
from discord.ext import commands

from aha.guildsettings import PROTECTED_COMMANDS, CommandDisabled, GuildSettings, GuildSettingsCache
from tests.helpers import FakeClock


class FakeBackend:
    """Records calls and holds one settings row per guild."""

    def __init__(self, rows: dict[int, GuildSettings] | None = None) -> None:
        self.rows = rows or {}
        self.calls: list[tuple[object, ...]] = []

    async def get(self, guild_id: int) -> GuildSettings | None:
        self.calls.append(("get", guild_id))
        return self.rows.get(guild_id)

    async def set_log_channel(self, guild_id: int, channel_id: int | None, by: int) -> None:
        self.calls.append(("log", guild_id, channel_id, by))

    async def disable(self, guild_id: int, command: str, by: int) -> None:
        self.calls.append(("disable", guild_id, command, by))

    async def enable(self, guild_id: int, command: str, by: int) -> None:
        self.calls.append(("enable", guild_id, command, by))

    async def delete(self, guild_id: int) -> bool:
        self.calls.append(("delete", guild_id))
        return self.rows.pop(guild_id, None) is not None


def gets(backend: FakeBackend) -> int:
    return sum(1 for call in backend.calls if call[0] == "get")


def test_a_guild_with_no_row_gets_the_defaults() -> None:
    cache = GuildSettingsCache(FakeBackend())

    settings = asyncio.run(cache.get(5))

    assert settings == GuildSettings(5)
    assert settings.disabled_commands == frozenset()
    assert settings.log_channel_id is None
    assert settings.locale is None


def test_reads_are_cached_until_the_ttl_elapses() -> None:
    clock = FakeClock(0.0)
    backend = FakeBackend({5: GuildSettings(5, log_channel_id=9)})
    cache = GuildSettingsCache(backend, ttl=60.0, clock=clock)

    first = asyncio.run(cache.get(5))
    clock.now = 59.9
    assert asyncio.run(cache.get(5)) is first
    assert gets(backend) == 1

    clock.now = 60.0  # exactly the TTL is already stale
    asyncio.run(cache.get(5))
    assert gets(backend) == 2


def test_a_missing_row_is_cached_too() -> None:
    backend = FakeBackend()
    cache = GuildSettingsCache(backend)

    asyncio.run(cache.get(5))
    asyncio.run(cache.get(5))

    assert gets(backend) == 1


def test_guilds_are_cached_separately() -> None:
    backend = FakeBackend({1: GuildSettings(1, log_channel_id=1), 2: GuildSettings(2)})
    cache = GuildSettingsCache(backend)

    assert asyncio.run(cache.get(1)).log_channel_id == 1
    assert asyncio.run(cache.get(2)).log_channel_id is None


def test_the_default_ttl_is_sixty_seconds() -> None:
    clock = FakeClock(0.0)
    backend = FakeBackend()
    cache = GuildSettingsCache(backend, clock=clock)

    asyncio.run(cache.get(5))
    clock.now = 59.0
    asyncio.run(cache.get(5))
    assert gets(backend) == 1
    clock.now = 60.0
    asyncio.run(cache.get(5))
    assert gets(backend) == 2


def test_every_write_reaches_the_store_and_drops_the_cached_entry() -> None:
    backend = FakeBackend()
    cache = GuildSettingsCache(backend)
    writes = [
        (cache.set_log_channel(5, 9, 7), ("log", 5, 9, 7)),
        (cache.disable(5, "roll", 7), ("disable", 5, "roll", 7)),
        (cache.enable(5, "roll", 7), ("enable", 5, "roll", 7)),
    ]
    for write, expected in writes:
        asyncio.run(cache.get(5))
        before = gets(backend)

        asyncio.run(write)
        asyncio.run(cache.get(5))

        assert expected in backend.calls
        assert gets(backend) == before + 1  # re-read, so the change is visible at once


def test_writing_before_anything_was_cached_is_fine() -> None:
    backend = FakeBackend()
    cache = GuildSettingsCache(backend)

    asyncio.run(cache.set_log_channel(5, 9, 7))
    asyncio.run(cache.disable(5, "roll", 7))
    asyncio.run(cache.enable(5, "roll", 7))
    asyncio.run(cache.delete(5))

    assert [call[0] for call in backend.calls] == ["log", "disable", "enable", "delete"]


def test_delete_removes_the_row_and_the_cached_entry() -> None:
    backend = FakeBackend({5: GuildSettings(5, log_channel_id=9)})
    cache = GuildSettingsCache(backend)
    asyncio.run(cache.get(5))

    assert asyncio.run(cache.delete(5)) is True
    assert asyncio.run(cache.get(5)) == GuildSettings(5)
    assert asyncio.run(cache.delete(5)) is False


def test_a_disabled_command_error_is_a_check_failure_on_both_surfaces() -> None:
    error = CommandDisabled("x")

    assert isinstance(error, app_commands.CheckFailure)
    assert isinstance(error, commands.CheckFailure)


def test_the_commands_that_must_stay_reachable_are_protected() -> None:
    assert {"config", "configure", "help"} == PROTECTED_COMMANDS
