"""Offline tests for syncing the command tree: diffing, skipping, dry runs, startup gating."""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

import discord
import pytest
from discord import app_commands

from aha.bot import COG_MODULES, OPERATOR_COG, AhaBot
from aha.config import Settings
from tests.helpers import http_error

GUILD_ID = 123456789012345678

#: Every command the harness loads (the meta and fun cogs), sorted. Add new commands here once.
ALL = ("8ball", "about", "coin", "countdown", "ping", "privacy", "roll", "time")
ALL_LINES = ", ".join(f"+ {name}" for name in ALL)
APPLICATION_ID = 555


def _remote_view(payload: dict[str, Any]) -> dict[str, Any]:
    """The raw payload Discord returns for a command we sent as ``payload``."""
    data = copy.deepcopy(payload)
    permissions = data.get("default_member_permissions")
    if permissions is not None:
        data["default_member_permissions"] = str(permissions)
    data.update({"id": "1", "application_id": str(APPLICATION_ID), "version": "2"})
    return data


class FakeHttp:
    """Stands in for ``HTTPClient``: serves canned command lists and records every fetch."""

    def __init__(
        self,
        global_commands: list[dict[str, Any]] | None = None,
        guild_commands: list[dict[str, Any]] | None = None,
    ) -> None:
        self.global_commands = global_commands or []
        self.guild_commands = guild_commands or []
        self.calls: list[tuple[str, int, int | None]] = []

    async def get_global_commands(self, application_id: int) -> list[dict[str, Any]]:
        self.calls.append(("global", application_id, None))
        return copy.deepcopy(self.global_commands)

    async def get_guild_commands(self, application_id: int, guild_id: int) -> list[dict[str, Any]]:
        self.calls.append(("guild", application_id, guild_id))
        return copy.deepcopy(self.guild_commands)


class Harness:
    """A bot with the meta cog loaded, fake REST, and a recording ``tree.sync``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, dev_guild_id: int | None = None) -> None:
        self.bot = AhaBot(Settings(token="test-token", dev_guild_id=dev_guild_id))
        self.bot._connection.application_id = APPLICATION_ID
        self.http = FakeHttp()
        self.sync_calls: list[dict[str, Any]] = []
        self.fail_sync_on: int | None = None
        monkeypatch.setattr(self.bot, "http", self.http)
        monkeypatch.setattr(self.bot.tree, "sync", self._sync)

    async def _sync(self, **kwargs: Any) -> list[Any]:
        self.sync_calls.append(kwargs)
        if self.fail_sync_on == len(self.sync_calls):
            raise http_error()
        return []

    def local(self, guild: discord.Object | None = None) -> list[dict[str, Any]]:
        return [c.to_dict(self.bot.tree) for c in self.bot.tree.get_commands(guild=guild)]

    def names(self) -> list[str]:
        return sorted(command.name for command in self.bot.tree.get_commands(guild=None))

    async def load(self) -> None:
        await self.bot.load_extension("aha.cogs.meta")
        await self.bot.load_extension("aha.cogs.fun")


def test_an_unchanged_global_tree_is_not_synced(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = Harness(monkeypatch)

    async def scenario() -> Any:
        await harness.load()
        harness.http.global_commands = [_remote_view(p) for p in harness.local()]
        return await harness.bot.sync_commands()

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        (result,) = asyncio.run(scenario())

    assert harness.sync_calls == []
    assert harness.http.calls == [("global", APPLICATION_ID, None)]
    assert (result.target, result.synced, result.diff.unchanged) == ("global", False, True)
    assert [r.getMessage() for r in caplog.records] == [
        "Skipped sync: global commands are unchanged"
    ]


def test_a_changed_global_tree_is_synced_and_the_changes_are_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = Harness(monkeypatch)

    async def scenario() -> Any:
        await harness.load()
        remote = [_remote_view(p) for p in harness.local() if p["name"] != "coin"]
        remote[0]["description"] = "Out of date."
        remote.append({**_remote_view(harness.local()[0]), "name": "gone"})
        harness.http.global_commands = remote
        return await harness.bot.sync_commands()

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        (result,) = asyncio.run(scenario())

    assert harness.sync_calls == [{}]  # one global sync, no guild argument
    assert result.synced is True
    assert result.diff.added == ("coin",)
    assert result.diff.removed == ("gone",)
    assert len(result.diff.changed) == 1
    assert [r.getMessage() for r in caplog.records] == [
        f"Synced global commands: {', '.join(result.diff.lines())}"
    ]


def test_a_dry_run_reports_changes_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = Harness(monkeypatch)

    async def scenario() -> Any:
        await harness.load()
        return await harness.bot.sync_commands(dry_run=True)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        (result,) = asyncio.run(scenario())

    assert harness.sync_calls == []
    assert result.target == "global"
    assert result.synced is False
    assert result.diff.added == ALL
    assert [r.getMessage() for r in caplog.records] == [
        f"Dry run: global commands would change: {ALL_LINES}"
    ]


def test_a_dry_run_of_an_unchanged_tree_says_unchanged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = Harness(monkeypatch)

    async def scenario() -> Any:
        await harness.load()
        harness.http.global_commands = [_remote_view(p) for p in harness.local()]
        return await harness.bot.sync_commands(dry_run=True)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        (result,) = asyncio.run(scenario())

    assert result.diff.unchanged is True
    assert harness.sync_calls == []
    assert [r.getMessage() for r in caplog.records] == [
        "Skipped sync: global commands are unchanged"
    ]


def test_dev_guild_sync_syncs_the_guild_and_skips_clean_globals(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    harness = Harness(monkeypatch, dev_guild_id=GUILD_ID)

    async def scenario() -> Any:
        await harness.load()
        return await harness.bot.sync_commands()

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        guild_result, global_result = asyncio.run(scenario())

    assert [call["guild"].id for call in harness.sync_calls] == [GUILD_ID]
    assert harness.http.calls == [
        ("guild", APPLICATION_ID, GUILD_ID),
        ("global", APPLICATION_ID, None),
    ]
    assert (guild_result.target, guild_result.synced) == (f"dev guild {GUILD_ID}", True)
    assert (global_result.target, global_result.synced) == ("global", False)
    assert harness.names() == list(ALL)  # globals restored locally
    assert [r.getMessage() for r in caplog.records] == [
        f"Synced dev guild {GUILD_ID} commands: {ALL_LINES}",
        "Skipped sync: global commands are unchanged",
    ]


def test_dev_guild_sync_purges_stale_remote_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, dev_guild_id=GUILD_ID)

    async def scenario() -> Any:
        await harness.load()
        harness.http.guild_commands = [_remote_view(p) for p in harness.local()]
        harness.http.global_commands = [_remote_view(p) for p in harness.local()]
        return await harness.bot.sync_commands()

    guild_result, global_result = asyncio.run(scenario())

    assert guild_result.synced is False  # the guild already matches
    assert global_result.synced is True
    assert global_result.diff.removed == ALL
    assert harness.sync_calls == [{}]  # only the global purge was written
    assert harness.names() == list(ALL)


def test_a_fully_synced_dev_guild_makes_no_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, dev_guild_id=GUILD_ID)

    async def scenario() -> Any:
        await harness.load()
        harness.http.guild_commands = [_remote_view(p) for p in harness.local()]
        return await harness.bot.sync_commands()

    results = asyncio.run(scenario())

    assert [r.synced for r in results] == [False, False]
    assert harness.sync_calls == []


def test_a_dev_guild_dry_run_writes_nothing_but_still_restores_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(monkeypatch, dev_guild_id=GUILD_ID)

    async def scenario() -> Any:
        await harness.load()
        harness.http.global_commands = [_remote_view(p) for p in harness.local()]
        return await harness.bot.sync_commands(dry_run=True)

    guild_result, global_result = asyncio.run(scenario())

    assert harness.sync_calls == []
    assert guild_result.diff.added == ALL
    assert global_result.diff.removed == ALL
    assert harness.names() == list(ALL)


def test_sync_commands_keeps_globals_for_a_second_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, dev_guild_id=GUILD_ID)

    async def scenario() -> tuple[list[str], list[str], list[str]]:
        await harness.load()
        before = harness.names()
        await harness.bot.sync_commands()
        after_first = harness.names()
        await harness.bot.sync_commands()
        return before, after_first, harness.names()

    before, after_first, after_second = asyncio.run(scenario())

    # The dev-guild purge empties the local global tree to empty the remote set;
    # it must restore it, or a later sync copies nothing and wipes the guild.
    assert before == list(ALL)
    assert after_first == before
    assert after_second == before


def test_sync_commands_restores_globals_when_the_purge_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(monkeypatch, dev_guild_id=GUILD_ID)
    harness.fail_sync_on = 2  # the global purge sync

    async def scenario() -> list[str]:
        await harness.load()
        harness.http.global_commands = [_remote_view(p) for p in harness.local()]
        with pytest.raises(discord.HTTPException):
            await harness.bot.sync_commands()
        return harness.names()

    assert asyncio.run(scenario()) == list(ALL)


def test_sync_commands_restores_globals_when_fetching_remote_globals_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(monkeypatch, dev_guild_id=GUILD_ID)

    async def failing_global_fetch(application_id: int) -> list[dict[str, Any]]:
        raise http_error()

    monkeypatch.setattr(harness.http, "get_global_commands", failing_global_fetch)

    async def scenario() -> list[str]:
        await harness.load()
        with pytest.raises(discord.HTTPException):
            await harness.bot.sync_commands()
        return harness.names()

    assert asyncio.run(scenario()) == list(ALL)


def test_sync_commands_serializes_concurrent_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, dev_guild_id=GUILD_ID)
    events: list[str] = []

    async def slow_sync(**kwargs: Any) -> list[Any]:
        events.append("start")
        await asyncio.sleep(0)
        events.append("end")
        return []

    monkeypatch.setattr(harness.bot.tree, "sync", slow_sync)

    async def scenario() -> None:
        await harness.load()
        harness.http.global_commands = [_remote_view(p) for p in harness.local()]
        await asyncio.gather(harness.bot.sync_commands(), harness.bot.sync_commands())

    asyncio.run(scenario())

    # Each call runs two syncs (guild, then global purge); the lock must keep the pairs apart.
    assert events == ["start", "end"] * 4


def test_sync_before_login_raises_missing_application_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(monkeypatch)
    harness.bot._connection.application_id = None

    with pytest.raises(discord.MissingApplicationID):
        asyncio.run(harness.bot.sync_commands())

    assert harness.http.calls == []
    assert harness.sync_calls == []


class _SetupRecorder:
    """Replaces ``load_extension`` and ``sync_commands`` to observe ``setup_hook``."""

    def __init__(self, bot: AhaBot, monkeypatch: pytest.MonkeyPatch) -> None:
        self.loaded: list[str] = []
        self.synced: list[bool] = []

        async def fake_load_extension(name: str) -> None:
            self.loaded.append(name)

        async def fake_sync_commands() -> list[Any]:
            self.synced.append(True)
            return []

        async def fake_super_close(self: Any) -> None:
            return None

        monkeypatch.setattr(bot, "load_extension", fake_load_extension)
        monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
        monkeypatch.setattr(type(bot).__mro__[1], "close", fake_super_close)

    async def run(self, bot: AhaBot) -> None:
        await bot.setup_hook()
        await bot.close()


def test_production_startup_loads_cogs_but_neither_syncs_nor_loads_the_operator_cog(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = AhaBot(Settings(token="test-token"))
    recorder = _SetupRecorder(bot, monkeypatch)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(recorder.run(bot))

    assert recorder.loaded == list(COG_MODULES)
    assert recorder.synced == []
    assert (bot.sync_command_enabled, bot.sync_on_startup) == (False, False)
    assert [r.getMessage() for r in caplog.records] == [
        *(f"Loaded extension {module}" for module in COG_MODULES),
        "Command sync skipped at startup (run 'aha sync')",
    ]


def test_a_dev_guild_syncs_at_startup_and_loads_the_operator_cog(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = AhaBot(Settings(token="test-token", dev_guild_id=GUILD_ID))
    recorder = _SetupRecorder(bot, monkeypatch)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(recorder.run(bot))

    assert recorder.loaded == [*COG_MODULES, OPERATOR_COG]
    assert recorder.synced == [True]
    assert (bot.sync_command_enabled, bot.sync_on_startup) == (True, True)
    assert caplog.records[-1].getMessage() == f"Loaded extension {OPERATOR_COG}"


def test_sync_on_startup_setting_syncs_and_loads_the_operator_cog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token", sync_on_startup=True))
    recorder = _SetupRecorder(bot, monkeypatch)

    asyncio.run(recorder.run(bot))

    assert recorder.loaded == [*COG_MODULES, OPERATOR_COG]
    assert recorder.synced == [True]


def test_the_sync_job_bot_keeps_the_operator_cog_but_skips_the_startup_sync(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = AhaBot(Settings(token="test-token", dev_guild_id=GUILD_ID), sync_on_startup=False)
    recorder = _SetupRecorder(bot, monkeypatch)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(recorder.run(bot))

    # The tree the Job syncs must match the tree the pods serve, so /sync stays registered.
    assert recorder.loaded == [*COG_MODULES, OPERATOR_COG]
    assert recorder.synced == []
    assert (bot.sync_command_enabled, bot.sync_on_startup) == (True, False)
    assert caplog.records[-1].getMessage() == "Command sync skipped at startup (run 'aha sync')"


def test_a_production_tree_has_no_sync_command(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario(settings: Settings) -> list[str]:
        bot = AhaBot(settings)
        for module in COG_MODULES:
            await bot.load_extension(module)
        if bot.sync_command_enabled:
            await bot.load_extension(OPERATOR_COG)
        return sorted(command.name for command in bot.tree.get_commands(guild=None))

    production = asyncio.run(scenario(Settings(token="test-token")))
    dev = asyncio.run(scenario(Settings(token="test-token", dev_guild_id=GUILD_ID)))

    assert "sync" not in production
    assert "sync" in dev


def test_the_guild_target_diffs_the_guilds_own_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = Harness(monkeypatch, dev_guild_id=GUILD_ID)

    async def only_here(interaction: discord.Interaction) -> None:
        return None

    async def scenario() -> Any:
        await harness.load()
        guild = discord.Object(id=GUILD_ID)
        harness.bot.tree.add_command(
            app_commands.Command(name="onlyhere", description="Dev only.", callback=only_here),
            guild=guild,
        )
        harness.http.global_commands = [_remote_view(p) for p in harness.local()]
        return await harness.bot.sync_commands()

    guild_result, _ = asyncio.run(scenario())

    assert guild_result.diff.added == tuple(sorted((*ALL, "onlyhere")))
