"""Tests for command metrics, readiness state, and the health-server wiring in the bot."""

from __future__ import annotations

import asyncio
import math
from typing import Any, ClassVar, cast

import pytest
from discord import app_commands
from discord.ext import commands

from aha.bot import HEALTH_TASK, READINESS_DISCONNECT_GRACE, AhaBot
from aha.cogs.observability import Observability, outcome_of
from aha.cogs.observability import setup as observability_setup
from aha.config import Settings
from aha.metrics import ShardState
from tests.helpers import (
    Named,
    StubContext,
    StubInteraction,
    as_context,
    as_interaction,
    as_tree_interaction,
)


def _bot(**settings: Any) -> AhaBot:
    return AhaBot(Settings(token="test-token", **settings))


def _count(bot: AhaBot, command: str, outcome: str) -> float | None:
    return bot.metrics.registry.get_sample_value(
        "aha_commands_total", {"command": command, "outcome": outcome}
    )


def test_a_completed_command_is_counted_ok() -> None:
    bot = _bot()

    asyncio.run(Observability(bot).on_command_completion(as_context(StubContext())))

    assert _count(bot, "ping", "ok") == 1


def test_a_completion_without_a_command_is_ignored() -> None:
    bot = _bot()

    asyncio.run(Observability(bot).on_command_completion(as_context(StubContext(command=False))))

    assert bot.metrics.registry.get_sample_value("aha_commands_total", {}) is None


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (commands.CommandOnCooldown(cast("Any", None), 1.0, commands.BucketType.user), "cooldown"),
        (app_commands.CommandOnCooldown(cast("Any", None), 1.0), "cooldown"),
        (commands.MissingPermissions(["manage_guild"]), "denied"),
        (app_commands.MissingPermissions(["manage_guild"]), "denied"),
        (commands.NotOwner(), "denied"),
        (commands.NoPrivateMessage(), "denied"),
        (commands.BadArgument("nope"), "error"),
        (RuntimeError("boom"), "error"),
    ],
)
def test_outcome_of_classifies_failures(error: Exception, outcome: str) -> None:
    assert outcome_of(error) == outcome


def test_outcome_of_looks_through_wrapper_exceptions() -> None:
    command = cast("Any", Named(name="x", qualified_name="x"))

    assert outcome_of(commands.CommandInvokeError(RuntimeError("boom"))) == "error"
    assert (
        outcome_of(commands.HybridCommandError(app_commands.MissingPermissions(["manage_guild"])))
        == "denied"
    )
    assert outcome_of(app_commands.CommandInvokeError(command, RuntimeError("boom"))) == "error"
    nested = app_commands.CommandInvokeError(
        command, commands.CommandOnCooldown(cast("Any", None), 1.0, commands.BucketType.user)
    )
    assert outcome_of(nested) == "cooldown"


def test_a_failed_message_or_hybrid_command_is_counted_by_outcome() -> None:
    bot = _bot()
    cog = Observability(bot)

    asyncio.run(cog.on_command_error(as_context(StubContext()), commands.NotOwner()))
    asyncio.run(cog.on_command_error(as_context(StubContext()), commands.BadArgument("x")))

    assert _count(bot, "ping", "denied") == 1
    assert _count(bot, "ping", "error") == 1
    assert _count(bot, "ping", "ok") is None


def test_an_unknown_command_is_not_counted() -> None:
    bot = _bot()
    ctx = StubContext(command=False)

    asyncio.run(Observability(bot).on_command_error(as_context(ctx), commands.CommandNotFound()))

    assert bot.metrics.registry.get_sample_value("aha_commands_total", {}) is None


def test_a_tree_level_failure_is_counted_with_its_command() -> None:
    bot = _bot()
    error = app_commands.CommandOnCooldown(cast("Any", None), 3.0)

    asyncio.run(Observability(bot).on_app_command_error(as_interaction(StubInteraction()), error))

    assert _count(bot, "ping", "cooldown") == 1


def test_a_tree_level_failure_without_a_command_is_ignored() -> None:
    bot = _bot()
    interaction = StubInteraction(command=False)
    error = app_commands.CommandOnCooldown(cast("Any", None), 3.0)

    asyncio.run(Observability(bot).on_app_command_error(as_interaction(interaction), error))

    assert bot.metrics.registry.get_sample_value("aha_commands_total", {}) is None


def test_the_rate_limit_rejection_of_a_slash_command_is_counted_with_a_duration() -> None:
    bot = AhaBot(Settings(token="test-token"), rate_limit=(1, 60.0))
    cog = Observability(bot)
    interaction = as_tree_interaction(StubInteraction())

    async def scenario() -> None:
        await bot.tree.interaction_check(interaction)
        with pytest.raises(app_commands.CommandOnCooldown) as caught:
            await bot.tree.interaction_check(interaction)
        await cog.on_app_command_error(interaction, caught.value)

    asyncio.run(scenario())

    assert _count(bot, "ping", "cooldown") == 1
    assert (
        bot.metrics.registry.get_sample_value(
            "aha_command_duration_seconds_count", {"command": "ping"}
        )
        == 1
    )


def test_a_mention_command_gets_a_duration_from_the_bot_check() -> None:
    bot = _bot()
    ctx = StubContext()
    cog = Observability(bot)

    async def scenario() -> None:
        await bot._enforce_rate_limit(as_context(ctx))
        await cog.on_command_completion(as_context(ctx))

    asyncio.run(scenario())

    assert _count(bot, "ping", "ok") == 1
    assert (
        bot.metrics.registry.get_sample_value(
            "aha_command_duration_seconds_count", {"command": "ping"}
        )
        == 1
    )


def test_setup_registers_the_cog_and_its_listeners_on_a_real_bot() -> None:
    bot = _bot()

    asyncio.run(observability_setup(bot))

    assert isinstance(bot.get_cog("Observability"), Observability)
    for event in ("on_command_completion", "on_command_error", "on_app_command_error"):
        assert len(bot.extra_events[event]) == 1


def test_setup_hook_loads_the_observability_cog(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()

    async def fake_super_close(self: Any) -> None:
        return None

    monkeypatch.setattr(commands.Bot, "close", fake_super_close)

    async def scenario() -> None:
        await bot.setup_hook()  # loads every real cog; production mode does not sync
        await bot.close()

    asyncio.run(scenario())

    # ``load_extension`` imports its own copy of the module, so compare by name, not class.
    assert bot.get_cog("Observability") is not None
    assert len(bot.extra_events["on_command_completion"]) == 1


def test_shard_states_report_not_ready_before_the_gateway_is_ready() -> None:
    bot = _bot()

    (state,) = bot.shard_states()

    assert state.shard_id == 0
    assert state.ready is False
    assert math.isnan(state.latency)


def test_shard_states_report_ready_with_the_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(type(bot), "latency", property(lambda self: 0.042))

    assert bot.shard_states() == [ShardState(0, True, 0.042)]


def test_a_short_disconnect_keeps_the_shard_ready_but_a_long_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    bot._disconnected_since = 100.0

    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 100.0 + READINESS_DISCONNECT_GRACE)
    assert bot.shard_states()[0].ready is True  # exactly at the grace limit

    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 100.0 + READINESS_DISCONNECT_GRACE + 0.5)
    assert bot.shard_states()[0].ready is False


def test_a_fresh_ready_ends_a_long_outage_so_the_shard_reports_ready_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(type(bot), "user", property(lambda self: Named(id=1)))
    bot._disconnected_since = 100.0
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 100.0 + 10 * READINESS_DISCONNECT_GRACE)
    assert bot.shard_states()[0].ready is False  # a long outage

    asyncio.run(bot.on_ready())  # discord could not resume and started a fresh session

    assert bot.shard_states()[0].ready is True


def test_a_connected_shard_that_is_not_ready_stays_unready_after_a_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    monkeypatch.setattr(bot, "is_ready", lambda: False)

    assert bot.shard_states()[0].ready is False


def test_guild_counts_group_cached_guilds_by_shard(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    guilds = [Named(shard_id=0), Named(shard_id=0), Named(shard_id=2)]
    monkeypatch.setattr(type(bot), "guilds", property(lambda self: guilds))

    assert bot.guild_counts() == {0: 2, 2: 1}


def test_an_owned_shard_without_guilds_reports_zero_rather_than_no_sample() -> None:
    bot = _bot()

    assert bot.guild_counts() == {0: 0}
    assert bot.metrics.registry.get_sample_value("aha_guilds", {"shard": "0"}) == 0


def test_a_disconnect_is_counted_as_a_reconnect() -> None:
    bot = _bot()

    asyncio.run(bot.on_disconnect())
    asyncio.run(bot.on_disconnect())

    assert (
        bot.metrics.registry.get_sample_value("aha_gateway_reconnects_total", {"shard": "0"}) == 2
    )


class _RecordingServer:
    """Replaces ``HealthServer`` to see how ``setup_hook`` constructs and starts it."""

    created: ClassVar[list[tuple[AhaBot, str, int]]] = []

    def __init__(self, bot: AhaBot, host: str, port: int) -> None:
        _RecordingServer.created.append((bot, host, port))

    async def run(self) -> None:
        await asyncio.Event().wait()


def _setup_hook(bot: AhaBot, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_super_close(self: Any) -> None:
        return None

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(commands.Bot, "close", fake_super_close)
    names: list[str] = []

    async def scenario() -> None:
        await bot.setup_hook()
        await bot.setup_hook()  # a second call must not start a second server
        names.extend(sorted(name for name in (HEALTH_TASK,) if name in bot.tasks))
        await bot.close()

    asyncio.run(scenario())
    return names


def test_setup_hook_starts_the_health_server_with_the_configured_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingServer.created.clear()
    monkeypatch.setattr("aha.bot.HealthServer", _RecordingServer)
    bot = _bot(health_bind="0.0.0.0", health_port=9100)

    running = _setup_hook(bot, monkeypatch)

    assert _RecordingServer.created == [(bot, "0.0.0.0", 9100)]
    assert running == [HEALTH_TASK]


def test_setup_hook_skips_the_health_server_when_the_bot_is_built_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingServer.created.clear()
    monkeypatch.setattr("aha.bot.HealthServer", _RecordingServer)
    bot = AhaBot(Settings(token="test-token", health_port=9100), health_server=False)

    running = _setup_hook(bot, monkeypatch)

    assert bot.health_server_enabled is False
    assert _RecordingServer.created == []
    assert running == []


def test_setup_hook_skips_the_health_server_when_the_port_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingServer.created.clear()
    monkeypatch.setattr("aha.bot.HealthServer", _RecordingServer)
    bot = _bot(health_port=0)

    running = _setup_hook(bot, monkeypatch)

    assert _RecordingServer.created == []
    assert running == []
