"""Smoke tests for :mod:`aha.bot` (no network/gateway connection)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, cast

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from aha.bot import COG_MODULES, AhaBot
from aha.config import Settings
from aha.ratelimit import RateLimiter
from tests.helpers import (
    FakeClock,
    Named,
    StubContext,
    StubInteraction,
    as_context,
    as_tree_interaction,
)


def _bot(**kwargs: Any) -> AhaBot:
    return AhaBot(Settings(token="test-token", **kwargs))


def test_bot_constructs_and_stores_settings() -> None:
    settings = Settings(token="test-token")
    bot = AhaBot(settings)

    assert bot.settings is settings
    assert bot.command_prefix is not None


def test_bot_requests_no_privileged_intents_and_has_no_default_help() -> None:
    bot = _bot()

    assert bot.intents.message_content is False
    assert bot.intents.members is False
    assert bot.intents.presences is False
    assert bot.intents.guilds is True  # discord.Intents.default() was used
    assert bot.help_command is None


def test_bot_passes_default_intents_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    # discord.py falls back to default intents for ``intents=None``, so only the
    # constructor argument itself distinguishes "explicit defaults" from "nothing".
    captured: dict[str, Any] = {}

    def fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self._checks = []

    monkeypatch.setattr(commands.Bot, "__init__", fake_init)

    _bot()

    assert captured["intents"] == discord.Intents.default()


def test_bot_prefix_is_mention_only(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "user", property(lambda self: _StubUser()))
    prefix = cast("Callable[[Any, Any], list[str]]", bot.command_prefix)

    assert prefix(bot, None) == ["<@123456789> ", "<@!123456789> "]


def test_cog_modules_are_configured_as_extensions() -> None:
    # Extensions are dotted module paths that must live under aha.cogs.
    assert COG_MODULES
    assert all(module.startswith("aha.cogs.") for module in COG_MODULES)


def test_setup_hook_loads_cogs_and_syncs_globally(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot()
    loaded: list[str] = []
    sync_calls: list[dict[str, Any]] = []

    async def fake_load_extension(name: str) -> None:
        loaded.append(name)

    async def fake_sync(**kwargs: Any) -> list[Any]:
        sync_calls.append(kwargs)
        return []

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(bot.tree, "sync", fake_sync)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.setup_hook())

    assert loaded == list(COG_MODULES)
    assert sync_calls == [{}]  # one global sync, no guild argument
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        *(f"Loaded extension {module}" for module in COG_MODULES),
        "Synced global application commands",
    ]


def test_setup_hook_syncs_dev_guild_then_clears_globals(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot(dev_guild_id=123456789012345678)
    sync_calls: list[dict[str, Any]] = []
    events: list[str] = []

    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_sync(**kwargs: Any) -> list[Any]:
        sync_calls.append(kwargs)
        events.append("sync")
        return []

    def fake_clear_commands(**kwargs: Any) -> None:
        events.append(f"clear:{kwargs}")

    def fake_copy_global_to(**kwargs: Any) -> None:
        events.append(f"copy:{kwargs['guild'].id}")

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(bot.tree, "sync", fake_sync)
    monkeypatch.setattr(bot.tree, "clear_commands", fake_clear_commands)
    monkeypatch.setattr(bot.tree, "copy_global_to", fake_copy_global_to)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.setup_hook())

    assert caplog.records[-1].getMessage() == ("Synced commands to dev guild 123456789012345678")
    assert len(sync_calls) == 2
    assert sync_calls[0]["guild"].id == 123456789012345678
    assert sync_calls[1] == {}  # global command set cleared
    # copy -> guild sync -> clear globals -> global sync, in that order.
    assert events == [
        "copy:123456789012345678",
        "sync",
        "clear:{'guild': None}",
        "sync",
    ]


def test_sync_commands_keeps_globals_for_a_second_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot(dev_guild_id=123456789012345678)

    async def fake_sync(**kwargs: Any) -> list[Any]:
        return []

    async def scenario() -> tuple[list[str], list[str], list[str]]:
        await bot.load_extension("aha.cogs.meta")
        monkeypatch.setattr(bot.tree, "sync", fake_sync)
        before = [command.name for command in bot.tree.get_commands(guild=None)]
        await bot.sync_commands()
        after_first = [command.name for command in bot.tree.get_commands(guild=None)]
        await bot.sync_commands()
        after_second = [command.name for command in bot.tree.get_commands(guild=None)]
        return before, after_first, after_second

    before, after_first, after_second = asyncio.run(scenario())

    # The dev-guild purge empties the local global tree to empty the remote set;
    # it must restore it, or a later sync copies nothing and wipes the guild.
    assert before == ["ping", "sync"]
    assert after_first == before
    assert after_second == before


def _gated_bot(clock: FakeClock) -> AhaBot:
    """Build a bot whose gate uses ``clock`` instead of the monotonic clock."""
    bot = AhaBot(Settings(token="test-token"), rate_limit=(1, 60.0))
    bot.rate_limiter = RateLimiter(1, 60.0, clock=clock)
    return bot


def test_global_rate_limit_consumes_for_message_contexts() -> None:
    clock = FakeClock()
    bot = _gated_bot(clock)
    ctx = StubContext()

    assert asyncio.run(bot._enforce_rate_limit(as_context(ctx))) is True
    with pytest.raises(commands.CommandOnCooldown) as caught:
        asyncio.run(bot._enforce_rate_limit(as_context(ctx)))
    assert caught.value.retry_after == pytest.approx(60.0)

    clock.now += 61.0

    assert asyncio.run(bot._enforce_rate_limit(as_context(ctx))) is True


def test_global_rate_limit_is_skipped_for_interaction_contexts() -> None:
    bot = _gated_bot(FakeClock())
    ctx = StubContext()
    ctx.interaction = Named(id=1)

    assert asyncio.run(bot._enforce_rate_limit(as_context(ctx))) is True
    assert asyncio.run(bot._enforce_rate_limit(as_context(ctx))) is True


def test_tree_interaction_check_enforces_the_shared_budget() -> None:
    clock = FakeClock()
    bot = _gated_bot(clock)
    interaction = as_tree_interaction(StubInteraction())

    assert asyncio.run(bot.tree.interaction_check(interaction)) is True
    with pytest.raises(app_commands.CommandOnCooldown) as caught:
        asyncio.run(bot.tree.interaction_check(interaction))
    assert caught.value.retry_after == pytest.approx(60.0)

    clock.now += 61.0

    assert asyncio.run(bot.tree.interaction_check(interaction)) is True


def test_global_rate_limit_is_wired_into_bot_checks() -> None:
    bot = AhaBot(Settings(token="test-token"), rate_limit=(1, 60.0))
    ctx = StubContext()

    assert asyncio.run(bot.can_run(as_context(ctx))) is True
    with pytest.raises(commands.CommandOnCooldown):
        asyncio.run(bot.can_run(as_context(ctx)))


class _StubUser:
    id = 123456789

    def __str__(self) -> str:
        return "stub#0001"


def test_on_ready_logs_when_user_is_available(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "user", property(lambda self: _StubUser()))

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_ready())

    # Assert the readiness message is actually emitted, so removing the log
    # call in ``on_ready`` fails this test.
    assert [r.getMessage() for r in caplog.records] == ["Logged in as stub#0001 (id=123456789)"]


def test_on_ready_logs_when_user_is_not_available(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "user", property(lambda self: None))

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_ready())

    # Assert that there is no on ready log received when no user is available
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (
            logging.ERROR,
            "on_ready fired but self.user is unset; bot state is inconsistent",
        )
    ]
