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

from aha import logctx
from aha.bot import COG_MODULES, HEALTH_TASK, WATCHDOG_TASK, AhaBot, gateway_intents
from aha.config import Settings
from aha.guildsettings import CommandDisabled
from aha.ratelimit import LocalBackend, RateLimiter
from aha.tasks import TaskRegistry
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
    assert bot.intents.guilds is True
    assert bot.help_command is None


def test_only_the_intents_the_bot_uses_are_requested() -> None:
    intents = gateway_intents()

    assert (
        intents.value == discord.Intents(guilds=True, guild_messages=True, dm_messages=True).value
    )
    enabled = {name for name, on in intents if on}
    assert enabled == {"guilds", "guild_messages", "dm_messages"}


def test_the_noisy_default_intents_are_not_requested() -> None:
    intents = gateway_intents()

    for name in (
        "guild_typing",
        "dm_typing",
        "voice_states",
        "invites",
        "emojis_and_stickers",
        "guild_reactions",
        "dm_reactions",
        "guild_scheduled_events",
        "auto_moderation",
        "integrations",
        "webhooks",
        "polls",
        "bans",
        "moderation",
        "expressions",
    ):
        assert getattr(intents, name) is False, name


def test_every_privileged_intent_stays_off() -> None:
    intents = gateway_intents()

    assert (intents.message_content, intents.members, intents.presences) == (False, False, False)


def test_bot_passes_its_minimal_intents_and_no_message_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # discord.py falls back to default intents for ``intents=None``, so only the
    # constructor argument itself distinguishes "explicit intents" from "nothing".
    captured: dict[str, Any] = {}

    def fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self._checks = []

    monkeypatch.setattr(commands.AutoShardedBot, "__init__", fake_init)

    _bot()

    assert captured["intents"] == gateway_intents()
    assert captured["intents"] != discord.Intents.default()
    assert captured["max_messages"] is None  # no per-channel message cache
    assert captured["member_cache_flags"] == discord.MemberCacheFlags.none()
    assert captured["chunk_guilds_at_startup"] is False


def test_bot_prefix_is_mention_only(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "user", property(lambda self: _StubUser()))
    prefix = cast("Callable[[Any, Any], list[str]]", bot.command_prefix)

    assert prefix(bot, None) == ["<@123456789> ", "<@!123456789> "]


def test_cog_modules_are_configured_as_extensions() -> None:
    # Extensions are dotted module paths that must live under aha.cogs.
    assert COG_MODULES
    assert all(module.startswith("aha.cogs.") for module in COG_MODULES)


def _gated_bot(clock: FakeClock) -> AhaBot:
    """Build a bot whose gate uses ``clock`` instead of the monotonic clock."""
    bot = AhaBot(Settings(token="test-token"), rate_limit=(1, 60.0))
    bot.rate_limiter = LocalBackend(RateLimiter(1, 60.0, clock=clock))
    return bot


def test_tree_interaction_check_binds_the_log_context() -> None:
    bot = _gated_bot(FakeClock())
    interaction = as_tree_interaction(StubInteraction())

    async def scenario() -> dict[str, object]:
        await bot.tree.interaction_check(interaction)
        return dict(logctx.current())

    assert asyncio.run(scenario()) == {
        "shard_id": 2,
        "guild_id": 11,
        "channel_id": 22,
        "user_id": 33,
        "interaction_id": 777,
        "command": "ping",
    }


def test_the_log_context_is_bound_even_when_the_rate_limit_rejects() -> None:
    bot = _gated_bot(FakeClock())
    interaction = as_tree_interaction(StubInteraction())

    async def scenario() -> dict[str, object]:
        await bot.tree.interaction_check(interaction)
        with pytest.raises(app_commands.CommandOnCooldown):
            await bot.tree.interaction_check(interaction)
        return dict(logctx.current())

    assert asyncio.run(scenario())["user_id"] == 33


def test_message_context_check_binds_the_log_context() -> None:
    bot = _gated_bot(FakeClock())
    ctx = StubContext()

    async def scenario() -> dict[str, object]:
        await bot._enforce_rate_limit(as_context(ctx))
        return dict(logctx.current())

    assert asyncio.run(scenario()) == {
        "shard_id": 2,
        "guild_id": 11,
        "channel_id": 22,
        "user_id": 33,
        "message_id": 44,
        "command": "ping",
    }


def test_message_context_check_leaves_slash_invocations_to_the_tree() -> None:
    bot = _gated_bot(FakeClock())
    ctx = StubContext()
    ctx.interaction = StubInteraction()

    async def scenario() -> dict[str, object]:
        await bot._enforce_rate_limit(as_context(ctx))
        return dict(logctx.current())

    assert asyncio.run(scenario()) == {}


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
    message = r"^gateway_timeout and watchdog_interval must be finite and positive$"
    for kwargs in (
        {"gateway_timeout": 0},
        {"gateway_timeout": -1},
        {"gateway_timeout": float("nan")},
        {"gateway_timeout": float("inf")},
        {"watchdog_interval": 0},
        {"watchdog_interval": -1},
        {"watchdog_interval": float("nan")},
        {"watchdog_interval": float("inf")},
    ):
        with pytest.raises(ValueError, match=message):
            AhaBot(Settings(token="test-token"), **kwargs)


def test_minimum_valid_watchdog_settings_are_accepted() -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=0.1, watchdog_interval=0.5)

    assert (bot.gateway_timeout, bot.watchdog_interval) == (0.1, 0.5)


def test_on_connect_logs_without_clearing_the_disconnect_clock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = _bot()
    bot._shard_down = {0: 123.0}

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_connect())

    # A resumed reconnect dispatches on_connect before on_resumed; the clock must
    # survive so on_resumed can report the outage duration.
    assert bot._shard_down == {0: 123.0}
    assert [record.getMessage() for record in caplog.records] == [
        "Connected to the Discord gateway"
    ]


def test_on_disconnect_logs_and_starts_the_clock_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot()
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 500.0)

    with caplog.at_level(logging.WARNING, logger="aha.bot"):
        asyncio.run(bot.on_shard_disconnect(0))
        monkeypatch.setattr("aha.bot.time.monotonic", lambda: 600.0)
        asyncio.run(bot.on_shard_disconnect(0))

    assert bot._shard_down == {0: 500.0}
    assert [record.getMessage() for record in caplog.records] == [
        "Shard 0 disconnected from the Discord gateway"
    ] * 2


def test_on_disconnect_logs_the_websocket_close_code(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = _bot()
    shard = Named(_parent=Named(ws=Named(_close_code=4000)))
    monkeypatch.setattr(type(bot), "shards", property(lambda self: {0: shard}))

    with caplog.at_level(logging.WARNING, logger="aha.bot"):
        asyncio.run(bot.on_shard_disconnect(0))

    assert [record.getMessage() for record in caplog.records] == [
        "Shard 0 disconnected from the Discord gateway (code 4000)"
    ]


def test_on_resumed_logs_the_outage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot()
    bot._shard_down = {0: 100.0}
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 102.5)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_shard_resumed(0))

    assert bot._shard_down == {}
    assert [record.getMessage() for record in caplog.records] == [
        "Shard 0 resumed the Discord gateway session after 2.5 s"
    ]


def test_resume_after_connect_keeps_the_outage_duration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot()
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 100.0)

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_shard_disconnect(0))
        asyncio.run(bot.on_connect())
        monkeypatch.setattr("aha.bot.time.monotonic", lambda: 102.5)
        asyncio.run(bot.on_shard_resumed(0))

    assert [record.getMessage() for record in caplog.records] == [
        "Shard 0 disconnected from the Discord gateway",
        "Connected to the Discord gateway",
        "Shard 0 resumed the Discord gateway session after 2.5 s",
    ]


def test_on_resumed_without_a_prior_disconnect(caplog: pytest.LogCaptureFixture) -> None:
    bot = _bot()

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_shard_resumed(0))

    assert [record.getMessage() for record in caplog.records] == [
        "Shard 0 resumed the Discord gateway session"
    ]


def test_on_shard_ready_logs_the_shard(caplog: pytest.LogCaptureFixture) -> None:
    bot = _bot()

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(bot.on_shard_ready(2))

    assert [record.getMessage() for record in caplog.records] == ["Shard 2 is ready"]


def test_a_shard_becoming_ready_clears_only_its_own_disconnect_clock() -> None:
    bot = _bot()
    bot._shard_down = {0: 123.0, 1: 456.0}

    asyncio.run(bot.on_shard_ready(0))

    assert bot._shard_down == {1: 456.0}


def test_check_gateway_timeout_allows_recent_disconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=60.0)
    bot._shard_down = {0: 100.0}
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    monkeypatch.setattr(bot, "close", fake_close)
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 159.0)

    assert asyncio.run(bot._check_gateway_timeout()) is False
    assert closed == []


def test_check_gateway_timeout_without_a_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    monkeypatch.setattr(bot, "close", fake_close)

    assert asyncio.run(bot._check_gateway_timeout()) is False
    assert closed == []


def test_check_gateway_timeout_closes_at_the_exact_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=60.0)
    bot._shard_down = {0: 100.0}
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    monkeypatch.setattr(bot, "close", fake_close)
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 160.0)

    assert asyncio.run(bot._check_gateway_timeout()) is True
    assert closed == [True]
    assert bot.exit_code == 1


def test_check_gateway_timeout_closes_when_the_limit_is_exceeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=60.0)
    bot._shard_down = {0: 100.0}
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    monkeypatch.setattr(bot, "close", fake_close)
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 161.0)

    with caplog.at_level(logging.CRITICAL, logger="aha.bot"):
        assert asyncio.run(bot._check_gateway_timeout()) is True

    assert closed == [True]
    assert bot.exit_code == 1
    assert [record.getMessage() for record in caplog.records] == [
        "Gateway has been disconnected for 61.0 s (limit 60.0 s); closing the bot"
    ]


def test_watch_gateway_checks_again_after_sleeping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token"), watchdog_interval=12.5)
    checks = 0
    waits: list[float] = []

    async def fake_check() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    async def fake_sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(bot, "_check_gateway_timeout", fake_check)
    monkeypatch.setattr("aha.bot.asyncio.sleep", fake_sleep)

    asyncio.run(bot._watch_gateway())

    assert (checks, waits) == (2, [12.5])


def test_watch_gateway_closes_when_disconnected_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token"), gateway_timeout=0.01)
    bot._shard_down = {0: 0.0}
    closed: list[bool] = []

    async def fake_close() -> None:
        closed.append(True)

    monkeypatch.setattr(bot, "close", fake_close)

    asyncio.run(bot._watch_gateway())

    assert closed == [True]
    assert bot.exit_code == 1


def test_setup_hook_schedules_the_watchdog_and_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    checked: list[bool] = []
    closed_client: list[bool] = []

    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_sync_commands() -> None:
        return None

    async def fake_check() -> bool:
        checked.append(True)
        return True

    async def fake_super_close(self: commands.AutoShardedBot) -> None:
        closed_client.append(True)

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
    monkeypatch.setattr(bot, "_check_gateway_timeout", fake_check)
    monkeypatch.setattr(commands.AutoShardedBot, "close", fake_super_close)

    async def scenario() -> tuple[bool, bool]:
        await bot.setup_hook()
        assert WATCHDOG_TASK in bot.tasks
        task = bot.tasks.get(WATCHDOG_TASK)
        assert task is not None
        await task
        await asyncio.sleep(0)  # let the done-callback unregister the finished task
        registered_after = WATCHDOG_TASK in bot.tasks
        await bot.close()
        return task.done(), registered_after

    assert asyncio.run(scenario()) == (True, False)
    assert checked == [True]
    assert closed_client == [True]


def test_close_cancels_a_running_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    closed_client: list[bool] = []

    async def fake_check() -> bool:
        return False

    async def fake_super_close(self: commands.AutoShardedBot) -> None:
        closed_client.append(True)

    monkeypatch.setattr(bot, "_check_gateway_timeout", fake_check)
    monkeypatch.setattr(commands.AutoShardedBot, "close", fake_super_close)

    async def scenario() -> bool:
        task = bot.tasks.spawn(bot._watch_gateway, name=WATCHDOG_TASK)
        await asyncio.sleep(0)  # let the task run its first check and sleep
        await bot.close()
        return task.cancelled()

    assert asyncio.run(scenario()) is True
    assert closed_client == [True]
    assert len(bot.tasks) == 0


def test_close_still_closes_the_client_when_waiting_for_tasks_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    closed_client: list[bool] = []

    async def cancelled_close(timeout: float = 0.0) -> None:
        raise asyncio.CancelledError

    async def fake_super_close(self: commands.AutoShardedBot) -> None:
        closed_client.append(True)

    monkeypatch.setattr(bot.tasks, "close", cancelled_close)
    monkeypatch.setattr(commands.AutoShardedBot, "close", fake_super_close)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.close())

    assert closed_client == [True]


def test_the_watchdog_is_restarted_when_it_crashes(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    sleeps: list[float] = []
    checks: list[int] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_sync_commands() -> None:
        return None

    async def flaky_check() -> bool:
        checks.append(len(checks) + 1)
        if len(checks) == 1:
            raise RuntimeError("watchdog bug")
        return True

    async def fake_super_close(self: commands.AutoShardedBot) -> None:
        return None

    bot.tasks = TaskRegistry(sleep=fake_sleep)
    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
    monkeypatch.setattr(bot, "_check_gateway_timeout", flaky_check)
    monkeypatch.setattr(commands.AutoShardedBot, "close", fake_super_close)

    async def scenario() -> None:
        await bot.setup_hook()
        for _ in range(10):
            await asyncio.sleep(0)
        await bot.close()

    asyncio.run(scenario())

    assert checks == [1, 2]  # crashed once, restarted, then finished normally
    assert sleeps == [1.0]
    assert bot.tasks.healthy is True


def test_a_cancelled_watchdog_marks_the_bot_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()

    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_sync_commands() -> None:
        return None

    async def fake_super_close(self: commands.AutoShardedBot) -> None:
        return None

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
    monkeypatch.setattr(commands.AutoShardedBot, "close", fake_super_close)

    async def scenario() -> frozenset[str]:
        await bot.setup_hook()
        task = bot.tasks.get(WATCHDOG_TASK)
        assert task is not None
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait({task})
        failed = bot.tasks.failed
        await bot.close()
        return failed

    assert asyncio.run(scenario()) == frozenset({WATCHDOG_TASK})


def test_close_without_a_watchdog_still_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    closed_client: list[bool] = []

    async def fake_super_close(self: commands.AutoShardedBot) -> None:
        closed_client.append(True)

    monkeypatch.setattr(commands.AutoShardedBot, "close", fake_super_close)

    asyncio.run(bot.close())

    assert closed_client == [True]
    assert len(bot.tasks) == 0


def test_setup_hook_does_not_start_a_second_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()

    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_sync_commands() -> None:
        return None

    async def fake_super_close(self: commands.AutoShardedBot) -> None:
        return None

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
    monkeypatch.setattr(commands.AutoShardedBot, "close", fake_super_close)

    async def scenario() -> tuple[asyncio.Task[None], asyncio.Task[None]]:
        await bot.setup_hook()
        first = bot.tasks.get(WATCHDOG_TASK)
        assert first is not None
        await bot.setup_hook()
        second = bot.tasks.get(WATCHDOG_TASK)
        assert second is not None
        await bot.close()
        return first, second

    first, second = asyncio.run(scenario())

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


def test_the_default_presence_is_listening_to_help_and_online() -> None:
    bot = _bot()

    activity = bot.activity
    assert isinstance(activity, discord.Activity)
    assert (activity.type, activity.name) == (discord.ActivityType.listening, "/help")
    assert bot.status is discord.Status.online


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("playing", discord.ActivityType.playing),
        ("listening", discord.ActivityType.listening),
        ("watching", discord.ActivityType.watching),
        ("competing", discord.ActivityType.competing),
    ],
)
def test_the_configured_presence_is_passed_to_the_client(
    kind: str, expected: discord.ActivityType
) -> None:
    settings = Settings(token="t", activity="with dice", activity_type=kind, status="dnd")

    bot = AhaBot(settings)

    activity = bot.activity
    # discord.py represents a "playing" activity as a ``Game``.
    assert isinstance(activity, (discord.Activity, discord.Game))
    assert (activity.type, activity.name) == (expected, "with dice")
    assert bot.status is discord.Status.dnd


def test_switching_the_activity_off_leaves_only_the_status() -> None:
    bot = AhaBot(Settings(token="t", activity=None, status="idle"))

    assert bot.activity is None
    assert bot.status is discord.Status.idle


# ------------------------------------------------------------ disabled commands (#98)


class _FakeSettings:
    def __init__(self, disabled: set[str], fail: bool = False) -> None:
        self.disabled = disabled
        self.fail = fail
        self.asked: list[int] = []

    async def get(self, guild_id: int) -> Any:
        self.asked.append(guild_id)
        if self.fail:
            raise RuntimeError("db down")
        return Named(disabled_commands=frozenset(self.disabled))


def _settings_bot(settings: _FakeSettings | None) -> AhaBot:
    bot = _bot()
    bot.guild_settings = cast("Any", settings)
    return bot


def test_a_disabled_command_is_refused_in_that_guild() -> None:
    settings = _FakeSettings({"ping"})
    bot = _settings_bot(settings)

    with pytest.raises(CommandDisabled) as caught:
        asyncio.run(bot.ensure_command_enabled(11, "ping"))

    assert str(caught.value) == "ping is disabled in this server"
    assert settings.asked == [11]


def test_disabling_a_group_disables_its_subcommands() -> None:
    bot = _settings_bot(_FakeSettings({"poll"}))

    with pytest.raises(CommandDisabled):
        asyncio.run(bot.ensure_command_enabled(11, "poll results"))


def test_an_enabled_command_and_the_other_commands_pass() -> None:
    bot = _settings_bot(_FakeSettings({"roll"}))

    asyncio.run(bot.ensure_command_enabled(11, "ping"))


def test_the_protected_commands_never_read_the_settings() -> None:
    settings = _FakeSettings({"help", "config"})
    bot = _settings_bot(settings)

    asyncio.run(bot.ensure_command_enabled(11, "help"))
    asyncio.run(bot.ensure_command_enabled(11, "config view"))

    assert settings.asked == []


@pytest.mark.parametrize(("guild_id", "command"), [(None, "ping"), (11, None)])
def test_nothing_to_check_without_a_guild_or_a_command(
    guild_id: int | None, command: str | None
) -> None:
    settings = _FakeSettings({"ping"})

    asyncio.run(_settings_bot(settings).ensure_command_enabled(guild_id, command))

    assert settings.asked == []


def test_without_a_database_every_command_is_enabled() -> None:
    asyncio.run(_settings_bot(None).ensure_command_enabled(11, "ping"))


def test_a_settings_outage_fails_open_and_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    bot = _settings_bot(_FakeSettings({"ping"}, fail=True))

    with caplog.at_level(logging.WARNING, logger="aha.bot"):
        asyncio.run(bot.ensure_command_enabled(11, "ping"))

    assert caplog.messages == ["Could not read guild settings; allowing the command"]
    assert isinstance(caplog.records[0].exc_info, tuple)
    assert str(caplog.records[0].exc_info[1]) == "db down"


def test_a_new_bot_has_no_guild_settings_until_a_database_is_connected() -> None:
    assert _bot().guild_settings is None


def test_the_slash_and_message_surfaces_both_enforce_it() -> None:
    slash, message = _gated_bot(FakeClock()), _gated_bot(FakeClock())
    slash.guild_settings = message.guild_settings = cast("Any", _FakeSettings({"ping"}))

    with pytest.raises(CommandDisabled):
        asyncio.run(slash.tree.interaction_check(as_tree_interaction(StubInteraction())))
    with pytest.raises(CommandDisabled):
        asyncio.run(message._enforce_rate_limit(as_context(StubContext())))


def test_a_command_without_a_guild_or_name_is_not_checked_on_either_surface() -> None:
    slash, message = _gated_bot(FakeClock()), _gated_bot(FakeClock())
    settings = _FakeSettings({"ping"})
    slash.guild_settings = message.guild_settings = cast("Any", settings)
    interaction = StubInteraction(command=False, guild=False)
    ctx = StubContext(command=False, guild=False)

    assert asyncio.run(slash.tree.interaction_check(as_tree_interaction(interaction))) is True
    assert asyncio.run(message._enforce_rate_limit(as_context(ctx))) is True
    assert settings.asked == []


# ----------------------------------------------------------------- shard assignment (#93)


def test_a_default_bot_lets_discord_choose_the_shards() -> None:
    bot = _bot()

    assert bot.shard_count is None
    assert bot.shard_ids is None


def test_the_configured_shard_assignment_reaches_discordpy_as_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self._checks = []

    monkeypatch.setattr(commands.AutoShardedBot, "__init__", fake_init)

    _bot(shard_count=8, shard_ids=(2, 3))

    assert captured["shard_count"] == 8
    assert captured["shard_ids"] == [2, 3]
    assert isinstance(captured["shard_ids"], list)  # discord.py rejects a tuple


def test_nothing_is_passed_for_an_unset_assignment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self._checks = []

    monkeypatch.setattr(commands.AutoShardedBot, "__init__", fake_init)

    _bot()

    assert "shard_count" not in captured
    assert "shard_ids" not in captured


def test_a_count_alone_is_passed_without_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_init(self: Any, **kwargs: Any) -> None:
        captured.update(kwargs)
        self._checks = []

    monkeypatch.setattr(commands.AutoShardedBot, "__init__", fake_init)

    _bot(shard_count=2)

    assert captured["shard_count"] == 2
    assert "shard_ids" not in captured


def test_real_discordpy_accepts_the_assignment_and_exposes_it() -> None:
    bot = _bot(shard_count=4, shard_ids=(1, 2))

    assert (bot.shard_count, bot.shard_ids) == (4, [1, 2])


# ------------------------------------------------- session budget and identify lock (#94)


class _Http:
    def __init__(self, limit: dict[str, int] | Exception) -> None:
        self.limit = limit
        self.calls = 0

    async def get_bot_gateway(self) -> tuple[int, str, Any]:
        self.calls += 1
        if isinstance(self.limit, Exception):
            raise self.limit
        return 1, "wss://gateway", self.limit


def _real_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests import conftest

    monkeypatch.setattr(AhaBot, "_guard_session_budget", conftest.REAL_GUARD)


def test_the_budget_guard_reads_discord_exports_metrics_and_learns_the_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _real_guard(monkeypatch)
    bot = _bot()
    http = _Http({"total": 1000, "remaining": 900, "reset_after": 60_000, "max_concurrency": 16})
    bot.http = cast("Any", http)

    asyncio.run(bot._guard_session_budget())

    registry = bot.metrics.registry
    assert registry.get_sample_value("aha_session_start_remaining") == 900
    assert registry.get_sample_value("aha_session_start_total") == 1000
    assert registry.get_sample_value("aha_session_start_reset_after_seconds") == 60.0
    assert bot.max_concurrency == 16


def test_a_low_budget_makes_the_bot_wait_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _real_guard(monkeypatch)
    bot = _bot(session_start_floor=0.5)  # not the default: 400 of 1000 is only low at this floor
    readings = [
        {"total": 1000, "remaining": 400, "reset_after": 120_000, "max_concurrency": 1},
        {"total": 1000, "remaining": 1000, "reset_after": 86_400_000, "max_concurrency": 1},
    ]
    waits: list[float] = []

    class Http:
        async def get_bot_gateway(self) -> tuple[int, str, Any]:
            return 1, "wss://gateway", readings.pop(0)

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    bot.http = cast("Any", Http())
    bot.budget_sleep = fake_sleep

    asyncio.run(bot._guard_session_budget())

    assert waits == [125.0]  # reset_after plus the margin, then it looked again
    assert readings == []


def test_an_unreadable_budget_does_not_block_startup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _real_guard(monkeypatch)
    bot = _bot()
    http = _Http(discord.ClientException("no gateway"))
    bot.http = cast("Any", http)

    with caplog.at_level(logging.WARNING, logger="aha.bot"):
        asyncio.run(bot._guard_session_budget())

    assert caplog.messages == ["Could not read the gateway session budget; continuing"]
    assert isinstance(caplog.records[0].exc_info, tuple)  # the cause is in the log
    assert bot.max_concurrency == 1


def test_the_budget_guard_waits_with_asyncio_sleep_unless_told_otherwise() -> None:
    assert _bot().budget_sleep is asyncio.sleep


def test_setup_hook_guards_the_budget_only_for_a_gateway_bot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_guard(self: Any) -> None:
        calls.append("guard")

    async def fake_load_extension(name: str) -> None:
        return None

    monkeypatch.setattr(AhaBot, "_guard_session_budget", fake_guard)
    for gateway in (True, False):
        bot = AhaBot(Settings(token="test-token", health_port=0), gateway=gateway)
        monkeypatch.setattr(bot, "load_extension", fake_load_extension)
        asyncio.run(bot.setup_hook())
        asyncio.run(bot.tasks.close())

    assert calls == ["guard"]  # the REST-only `aha sync` Job never touches the gateway budget


class _Coordinator:
    def __init__(self) -> None:
        self.buckets: list[int] = []

    async def acquire(self, bucket: int) -> None:
        self.buckets.append(bucket)


def test_the_identify_hook_takes_the_lock_for_the_shards_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    coordinator = _Coordinator()
    bot.identify = cast("Any", coordinator)
    bot.max_concurrency = 4
    defaults: list[Any] = []

    async def fake_super(self: Any, shard_id: Any, *, initial: bool = False) -> None:
        defaults.append(shard_id)

    monkeypatch.setattr(commands.AutoShardedBot, "before_identify_hook", fake_super)

    asyncio.run(bot.before_identify_hook(6))
    asyncio.run(bot.before_identify_hook(3, initial=True))

    assert coordinator.buckets == [2, 3]  # shard_id % max_concurrency
    assert defaults == []  # discord.py's own 5 s sleep is replaced, not added


def test_without_coordination_discordpys_own_hook_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    seen: list[tuple[Any, bool]] = []

    async def fake_super(self: Any, shard_id: Any, *, initial: bool = False) -> None:
        seen.append((shard_id, initial))

    monkeypatch.setattr(commands.AutoShardedBot, "before_identify_hook", fake_super)

    asyncio.run(bot.before_identify_hook(2, initial=True))
    bot.identify = cast("Any", _Coordinator())
    asyncio.run(bot.before_identify_hook(None))  # a non-sharded client has no shard id

    assert seen == [(2, True), (None, False)]


def test_the_health_server_is_already_up_while_the_budget_guard_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []

    async def waiting_guard(self: AhaBot) -> None:
        seen.append(HEALTH_TASK in self.tasks)  # a liveness probe must be answerable now

    async def fake_load_extension(name: str) -> None:
        return None

    monkeypatch.setattr(AhaBot, "_guard_session_budget", waiting_guard)
    bot = AhaBot(Settings(token="test-token", health_port=8080))
    monkeypatch.setattr(bot, "load_extension", fake_load_extension)

    async def scenario() -> None:
        await bot.setup_hook()
        await bot.tasks.close()

    asyncio.run(scenario())

    assert seen == [True]


def test_a_real_bot_keeps_no_member_cache_and_does_not_chunk_guilds() -> None:
    bot = _bot()

    assert bot._connection.member_cache_flags == discord.MemberCacheFlags.none()
    assert bot._connection._chunk_guilds is False


def test_dropping_mention_commands_removes_every_message_intent() -> None:
    intents = gateway_intents(mention_commands=False)

    assert {name for name, on in intents if on} == {"guilds"}
    assert intents.guild_messages is False
    assert intents.dm_messages is False


def test_mention_commands_are_on_by_default_and_the_setting_reaches_the_intents() -> None:
    assert gateway_intents().guild_messages is True
    assert gateway_intents(True).dm_messages is True
    assert _bot().intents.guild_messages is True
    quiet = _bot(mention_commands=False)
    assert (quiet.intents.guild_messages, quiet.intents.dm_messages) == (False, False)
    assert quiet.intents.guilds is True  # the guild cache and on_guild_remove still work


def test_every_command_and_menu_is_installable_by_servers_only() -> None:
    """Without this Discord uses the portal setting, which can include user installs."""
    bot = _bot()

    async def payloads() -> list[dict[str, Any]]:
        for module in COG_MODULES:
            await bot.load_extension(module)
        return [command.to_dict(bot.tree) for command in bot.tree.get_commands()]

    found = asyncio.run(payloads())

    assert len(found) >= 20  # every slash command and context menu, nothing skipped
    assert {tuple(payload["integration_types"]) for payload in found} == {(0,)}
    assert bot.tree.allowed_installs.guild is True
    assert bot.tree.allowed_installs.user is False
