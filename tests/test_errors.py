"""Offline tests for global command error handling (no Discord connection)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest
from discord import app_commands
from discord.ext import commands

from aha.bot import AhaBot
from aha.cogs.errors import Errors
from aha.cogs.errors import setup as errors_setup
from aha.config import Settings
from tests.helpers import (
    Named,
    StubContext,
    StubInteraction,
    as_context,
    as_interaction,
    as_tree_interaction,
    http_error,
)


def _cog() -> Errors:
    return Errors(cast("commands.Bot", object()))


def test_prefix_expected_error_gets_targeted_ephemeral_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = StubContext()
    error = commands.MissingPermissions(["manage_messages"])

    with caplog.at_level(logging.DEBUG, logger="aha"):
        asyncio.run(_cog().on_command_error(as_context(ctx), error))

    assert ctx.replies == [
        ("You need these permissions to do that: manage messages.", {"ephemeral": True})
    ]
    assert caplog.records == []


def test_prefix_unexpected_error_is_logged_with_context_and_hides_internals(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = StubContext()
    boom = RuntimeError("secret /path/to/file.py")
    error = commands.CommandInvokeError(boom)

    with caplog.at_level(logging.ERROR, logger="aha.cogs.errors"):
        asyncio.run(_cog().on_command_error(as_context(ctx), error))

    (record,) = caplog.records
    assert record.args is not None
    error_id = cast("str", cast("tuple[Any, ...]", record.args)[1])
    assert len(error_id) == 8
    assert record.getMessage() == (
        f"Unexpected error in command ping (error_id={error_id} guild=11 channel=22 user=33)"
    )
    assert cast("tuple[Any, ...]", record.args) == ("ping", error_id, 11, 22, 33)
    assert record.exc_info is not None
    assert record.exc_info[1] is boom
    assert ctx.replies == [
        (
            f"Something went wrong. If it keeps happening, quote error ID `{error_id}`.",
            {"ephemeral": True},
        )
    ]
    assert "secret" not in ctx.replies[0][0]


def test_unknown_command_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    ctx = StubContext(command=False)

    with caplog.at_level(logging.DEBUG, logger="aha.cogs.errors"):
        asyncio.run(_cog().on_command_error(as_context(ctx), commands.CommandNotFound()))

    assert ctx.replies == []
    assert [r.getMessage() for r in caplog.records] == ["Ignoring unknown command from user 33"]


def test_prefix_dm_and_missing_command_context_are_logged_as_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = StubContext(command=False, guild=False)

    with caplog.at_level(logging.ERROR, logger="aha.cogs.errors"):
        asyncio.run(_cog().on_command_error(as_context(ctx), commands.CommandError("x")))

    (record,) = caplog.records
    assert record.args is not None
    args = cast("tuple[Any, ...]", record.args)
    assert (args[0], args[2], args[3], args[4]) == (None, None, 22, 33)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            commands.BotMissingPermissions(["send_messages"]),
            "I'm missing permissions to do that: send messages.",
        ),
        (
            commands.MissingPermissions(["manage_messages", "kick_members"]),
            "You need these permissions to do that: manage messages, kick members.",
        ),
        (commands.MissingRole("Admin"), "You don't have the role required to do that."),
        (
            commands.CommandOnCooldown(commands.Cooldown(1, 5), 2.5, commands.BucketType.user),
            "That command is on cooldown. Try again in 2.5s.",
        ),
        (commands.NoPrivateMessage(), "This command can't be used in direct messages."),
        (commands.CheckFailure(), "You're not allowed to use that command."),
        (
            app_commands.MissingPermissions(["kick_members"]),
            "You need these permissions to do that: kick members.",
        ),
        (
            app_commands.BotMissingPermissions(["kick_members"]),
            "I'm missing permissions to do that: kick members.",
        ),
        (app_commands.MissingRole("Admin"), "You don't have the role required to do that."),
        (app_commands.NoPrivateMessage(), "This command can't be used in direct messages."),
        (app_commands.CheckFailure(), "You're not allowed to use that command."),
    ],
)
def test_expected_errors_map_to_messages(error: Exception, expected: str) -> None:
    ctx = StubContext()

    asyncio.run(_cog().on_command_error(as_context(ctx), cast("commands.CommandError", error)))

    assert ctx.replies == [(expected, {"ephemeral": True})]


def test_app_cooldown_message() -> None:
    interaction = StubInteraction()
    error = app_commands.CommandOnCooldown(app_commands.Cooldown(1, 5), 4.0)

    asyncio.run(_cog().on_app_command_error(as_interaction(interaction), error))

    assert interaction.sent == [
        ("response", "That command is on cooldown. Try again in 4.0s.", {"ephemeral": True})
    ]


def test_app_unexpected_error_replies_and_logs_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    interaction = StubInteraction()
    boom = ValueError("nope")
    command = cast("Any", Named(name="ping", qualified_name="ping"))
    error = app_commands.CommandInvokeError(command, boom)

    with caplog.at_level(logging.ERROR, logger="aha.cogs.errors"):
        asyncio.run(_cog().on_app_command_error(as_interaction(interaction), error))

    (record,) = caplog.records
    assert record.args is not None
    error_id = cast("str", cast("tuple[Any, ...]", record.args)[1])
    assert record.getMessage() == (
        f"Unexpected error in command ping (error_id={error_id} guild=11 channel=22 user=33)"
    )
    assert cast("tuple[Any, ...]", record.args) == ("ping", error_id, 11, 22, 33)
    assert record.exc_info is not None
    assert record.exc_info[1] is boom
    assert interaction.sent == [
        (
            "response",
            f"Something went wrong. If it keeps happening, quote error ID `{error_id}`.",
            {"ephemeral": True},
        )
    ]


def test_app_error_uses_followup_when_response_already_sent() -> None:
    interaction = StubInteraction(done=True, command=False)

    asyncio.run(
        _cog().on_app_command_error(as_interaction(interaction), app_commands.CheckFailure())
    )

    assert interaction.sent == [
        ("followup", "You're not allowed to use that command.", {"ephemeral": True})
    ]


def test_hybrid_wrapper_is_unwrapped(caplog: pytest.LogCaptureFixture) -> None:
    ctx = StubContext()
    error = commands.HybridCommandError(
        app_commands.CommandInvokeError(
            cast("Any", Named(name="ping", qualified_name="ping")), commands.NoPrivateMessage()
        )
    )

    asyncio.run(_cog().on_command_error(as_context(ctx), error))

    assert ctx.replies == [("This command can't be used in direct messages.", {"ephemeral": True})]
    assert caplog.records == []


def test_failed_reply_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    ctx = StubContext()
    ctx.fail_with = http_error()

    with caplog.at_level(logging.WARNING, logger="aha.cogs.errors"):
        asyncio.run(_cog().on_command_error(as_context(ctx), commands.CheckFailure()))

    (record,) = caplog.records
    assert record.getMessage() == "Could not deliver error reply for command ping"
    assert record.levelno == logging.WARNING
    # ``exc_info=False`` would leave a falsy-but-not-None value, so check truthiness.
    assert record.exc_info
    assert record.exc_info[1] is ctx.fail_with


def test_setup_registers_listeners_on_a_real_bot() -> None:
    bot = AhaBot(Settings(token="test-token"))

    asyncio.run(errors_setup(bot))

    cog = bot.get_cog("Errors")
    assert isinstance(cog, Errors)
    assert cog.bot is bot
    assert len(bot.extra_events["on_command_error"]) == 1
    assert len(bot.extra_events["on_app_command_error"]) == 1


def test_tree_forwards_errors_to_bot_when_a_listener_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token"))
    dispatched: list[tuple[Any, ...]] = []
    fallback: list[Any] = []

    async def listener(*_: Any) -> None:  # pragma: no cover - never invoked, dispatch is stubbed
        pass

    async def fake_default(*args: Any) -> None:
        fallback.append(args)

    bot.add_listener(listener, "on_app_command_error")
    monkeypatch.setattr(bot, "dispatch", lambda *args: dispatched.append(args))
    monkeypatch.setattr(app_commands.CommandTree, "on_error", fake_default)
    interaction, error = as_tree_interaction(StubInteraction()), app_commands.CheckFailure()

    asyncio.run(bot.tree.on_error(interaction, error))

    assert dispatched == [("app_command_error", interaction, error)]
    assert fallback == []


def test_tree_falls_back_to_default_logging_without_a_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = AhaBot(Settings(token="test-token"))
    dispatched: list[tuple[Any, ...]] = []
    fallback: list[tuple[Any, ...]] = []

    async def fake_default(*args: Any) -> None:
        fallback.append(args)

    monkeypatch.setattr(bot, "dispatch", lambda *args: dispatched.append(args))
    monkeypatch.setattr(app_commands.CommandTree, "on_error", fake_default)
    interaction, error = as_tree_interaction(StubInteraction()), app_commands.CheckFailure()

    asyncio.run(bot.tree.on_error(interaction, error))

    assert dispatched == []
    assert fallback == [(bot.tree, interaction, error)]
