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
    http_error,
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


def test_sync_commands_restores_globals_when_the_purge_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot(dev_guild_id=123456789012345678)
    calls: list[dict[str, Any]] = []

    async def fake_sync(**kwargs: Any) -> list[Any]:
        calls.append(kwargs)
        if len(calls) == 2:  # the global purge sync
            raise http_error()
        return []

    async def scenario() -> list[str]:
        await bot.load_extension("aha.cogs.meta")
        monkeypatch.setattr(bot.tree, "sync", fake_sync)
        with pytest.raises(discord.HTTPException):
            await bot.sync_commands()
        return [command.name for command in bot.tree.get_commands(guild=None)]

    assert asyncio.run(scenario()) == ["ping", "sync"]


def test_sync_commands_serializes_concurrent_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(dev_guild_id=123456789012345678)
    events: list[str] = []

    async def fake_sync(**kwargs: Any) -> list[Any]:
        events.append("start")
        await asyncio.sleep(0)
        events.append("end")
        return []

    async def scenario() -> None:
        await bot.load_extension("aha.cogs.meta")
        monkeypatch.setattr(bot.tree, "sync", fake_sync)
        await asyncio.gather(bot.sync_commands(), bot.sync_commands())

    asyncio.run(scenario())

    # Each call runs two syncs; the lock must keep the pairs from interleaving.
    assert events == ["start", "end"] * 4


def test_global_rate_limit_consumes_for_message_contexts() -> None:
    clock = FakeClock()
    bot = _gated_bot(clock)
    ctx = StubContext()

    assert asyncio.run(bot._enforce_rate_limit(as_context(ctx))) is True
    with pytest.raises(commands.CommandOnCooldown) as caught:
        asyncio.run(bot._enforce_rate_limit(as_context(ctx)))
    assert caught.value.retry_after == pytest.approx(60.0)
    assert caught.value.cooldown is not None
    assert (caught.value.cooldown.rate, caught.value.cooldown.per) == (1, 60.0)
    assert caught.value.type is commands.BucketType.user

    other = StubContext()
    other.author = Named(id=44)
    assert asyncio.run(bot._enforce_rate_limit(as_context(other))) is True

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
    assert caught.value.cooldown is not None
    assert (caught.value.cooldown.rate, caught.value.cooldown.per) == (1, 60.0)

    other = as_tree_interaction(StubInteraction(user_id=2))
    assert asyncio.run(bot.tree.interaction_check(other)) is True

    clock.now += 61.0

    assert asyncio.run(bot.tree.interaction_check(interaction)) is True


def test_global_rate_limit_is_wired_into_bot_checks() -> None:
    bot = AhaBot(Settings(token="test-token"), rate_limit=(1, 60.0))
    ctx = StubContext()

    assert asyncio.run(bot.can_run(as_context(ctx))) is True
    with pytest.raises(commands.CommandOnCooldown):
        asyncio.run(bot.can_run(as_context(ctx)))


def test_invalid_watchdog_settings_are_rejected() -> None:
    message = r"^gateway_timeout and watchdog_interval must be positive$"
    with pytest.raises(ValueError, match=message):
        AhaBot(Settings(token="test-token"), gateway_timeout=0)
    with pytest.raises(ValueError, match=message):
        AhaBot(Settings(token="test-token"), gateway_timeout=-1)
    with pytest.raises(ValueError, match=message):
        AhaBot(Settings(token="test-token"), watchdog_interval=0)
    with pytest.raises(ValueError, match=message):
        AhaBot(Settings(token="test-token"), watchdog_interval=-1)


def test_minimum_valid_watchdog_settings_are_accepted() -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=0.1, watchdog_interval=0.5)

    assert (bot.gateway_timeout, bot.watchdog_interval) == (0.1, 0.5)


def test_on_connect_logs_and_clears_the_disconnect_clock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = _bot()
    bot._disconnected_since = 123.0

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_connect())

    assert bot._disconnected_since is None
    assert [record.getMessage() for record in caplog.records] == [
        "Connected to the Discord gateway"
    ]


def test_on_disconnect_logs_and_starts_the_clock_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot()
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 500.0)

    with caplog.at_level(logging.WARNING, logger="aha.bot"):
        asyncio.run(bot.on_disconnect())
        monkeypatch.setattr("aha.bot.time.monotonic", lambda: 600.0)
        asyncio.run(bot.on_disconnect())

    assert bot._disconnected_since == 500.0
    assert [record.getMessage() for record in caplog.records] == [
        "Disconnected from the Discord gateway"
    ] * 2


def test_on_resumed_logs_the_outage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot()
    bot._disconnected_since = 100.0
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 102.5)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_resumed())

    assert bot._disconnected_since is None
    assert [record.getMessage() for record in caplog.records] == [
        "Resumed the Discord gateway session after 2.5 s"
    ]


def test_on_resumed_without_a_prior_disconnect(caplog: pytest.LogCaptureFixture) -> None:
    bot = _bot()

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_resumed())

    assert [record.getMessage() for record in caplog.records] == [
        "Resumed the Discord gateway session"
    ]


def test_check_gateway_timeout_allows_recent_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=60.0)
    bot._disconnected_since = 100.0
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 159.0)

    assert asyncio.run(bot._check_gateway_timeout()) is False


def test_check_gateway_timeout_without_a_disconnect() -> None:
    bot = _bot()

    assert asyncio.run(bot._check_gateway_timeout()) is False


def test_check_gateway_timeout_closes_at_the_exact_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=60.0)
    bot._disconnected_since = 100.0
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    monkeypatch.setattr(bot, "close", fake_close)
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 160.0)

    assert asyncio.run(bot._check_gateway_timeout()) is True
    assert closed == [True]


def test_check_gateway_timeout_closes_when_the_limit_is_exceeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=60.0)
    bot._disconnected_since = 100.0
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    monkeypatch.setattr(bot, "close", fake_close)
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 161.0)

    with caplog.at_level(logging.CRITICAL, logger="aha.bot"):
        assert asyncio.run(bot._check_gateway_timeout()) is True

    assert closed == [True]
    assert bot._watchdog_task is None
    assert [record.getMessage() for record in caplog.records] == [
        "Gateway has been disconnected for 61.0 s (limit 60.0 s); closing the bot"
    ]


def test_watch_gateway_sleeps_between_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = AhaBot(Settings(token="test-token"), watchdog_interval=12.5)
    waits: list[float] = []

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr("aha.bot.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._watch_gateway())

    assert waits == [12.5]


def test_watch_gateway_closes_when_disconnected_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=0.01)
    bot._disconnected_since = 0.0
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    monkeypatch.setattr(bot, "close", fake_close)

    asyncio.run(bot._watch_gateway())

    assert closed == [True]


def test_setup_hook_starts_the_watchdog_and_close_cancels_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()

    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_sync_commands() -> None:
        return None

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)

    async def scenario() -> bool:
        await bot.setup_hook()
        task = bot._watchdog_task
        assert task is not None
        await bot.close()
        return task.cancelled()

    assert asyncio.run(scenario()) is True
    assert bot._watchdog_task is None


def test_close_without_a_watchdog_is_a_no_op() -> None:
    bot = _bot()

    asyncio.run(bot.close())

    assert bot._watchdog_task is None


def test_setup_hook_does_not_start_a_second_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()

    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_sync_commands() -> None:
        return None

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)

    async def scenario() -> tuple[asyncio.Task[None] | None, asyncio.Task[None] | None]:
        await bot.setup_hook()
        first = bot._watchdog_task
        await bot.setup_hook()
        second = bot._watchdog_task
        await bot.close()
        return first, second

    first, second = asyncio.run(scenario())

    assert first is not None
    assert first is second
    assert first.get_name() == "gateway-watchdog"


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
