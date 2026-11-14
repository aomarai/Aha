"""Offline tests for the operator-only ``/sync`` command."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.utils import maybe_coroutine

from aha.bot import AhaBot, SyncResult
from aha.cmdsync import CommandDiff
from aha.cogs.ops import Operator
from aha.cogs.ops import setup as ops_setup
from aha.config import Settings
from tests.helpers import Named, StubContext, as_context, assert_reply


def _bot() -> AhaBot:
    return AhaBot(Settings(token="test-token"))


def _run(cog: Operator, ctx: StubContext) -> None:
    callback = cast("Any", cog.sync.callback)
    asyncio.run(callback(cog, as_context(ctx)))


async def _run_checks(ctx: Any) -> list[Any]:
    return [await maybe_coroutine(check, ctx) for check in Operator.sync.checks]


def _ctx(*, manage_guild: bool = True, owner: bool = True) -> StubContext:
    ctx = StubContext()
    # ``commands.has_permissions`` reads the cached ``Context.permissions``.
    ctx.permissions = discord.Permissions(manage_guild=manage_guild)

    async def is_owner(user: object) -> bool:
        return owner

    ctx.bot = Named(is_owner=is_owner)
    return ctx


def _results(*, synced: bool) -> list[SyncResult]:
    diff = CommandDiff(added=("ping",)) if synced else CommandDiff()
    return [SyncResult("global", diff, synced=synced)]


def test_sync_delegates_to_the_bot_and_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    calls: list[dict[str, Any]] = []

    async def fake_sync_commands(**kwargs: Any) -> list[SyncResult]:
        calls.append(kwargs)
        return _results(synced=True)

    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
    ctx = StubContext()

    _run(Operator(bot), ctx)

    assert calls == [{}]  # a real sync, not a dry run
    assert ctx.deferred == [True]
    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.description == "Application commands synced."
    assert embed.color is not None
    assert embed.color.value == 0x57F287  # success green


def test_sync_says_so_when_nothing_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()

    async def fake_sync_commands() -> list[SyncResult]:
        return _results(synced=False)

    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
    ctx = StubContext()

    _run(Operator(bot), ctx)

    assert ctx.replies[0][1]["embed"].description == "Application commands are already up to date."


def test_sync_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()

    async def fake_sync_commands() -> list[SyncResult]:
        return _results(synced=True)

    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
    ctx = StubContext(reject_embeds=True)

    _run(Operator(bot), ctx)

    assert_reply(ctx.replies, "Application commands synced.")
    assert ctx.deferred == [True]


def test_sync_metadata_and_guild_only_manage_guild_gate() -> None:
    assert Operator.sync.qualified_name == "sync"
    assert (
        Operator.sync.description
        == "Re-sync the slash commands (bot owner, requires Manage Server)."
    )
    app_command = Operator.sync.app_command
    assert app_command is not None
    assert app_command.guild_only is True
    assert app_command.default_permissions is not None
    assert app_command.default_permissions.manage_guild is True


def test_sync_checks_accept_the_owner_with_manage_guild() -> None:
    assert asyncio.run(_run_checks(_ctx())) == [True, True, True]


def test_sync_checks_reject_a_member_without_manage_guild() -> None:
    with pytest.raises(commands.MissingPermissions) as caught:
        asyncio.run(_run_checks(_ctx(manage_guild=False)))
    assert caught.value.missing_permissions == ["manage_guild"]


def test_sync_checks_reject_a_non_owner_even_with_manage_guild() -> None:
    with pytest.raises(commands.NotOwner):
        asyncio.run(_run_checks(_ctx(owner=False)))


def test_sync_checks_reject_direct_messages() -> None:
    ctx = _ctx()
    ctx.guild = None
    with pytest.raises(commands.NoPrivateMessage):
        asyncio.run(_run_checks(ctx))


def test_sync_app_check_requires_manage_guild() -> None:
    app_command = Operator.sync.app_command
    assert app_command is not None
    (check,) = app_command.checks
    denied = cast("Any", Named(permissions=discord.Permissions.none()))
    with pytest.raises(app_commands.MissingPermissions) as caught:
        check(denied)
    assert caught.value.missing_permissions == ["manage_guild"]
    allowed = cast("Any", Named(permissions=discord.Permissions(manage_guild=True)))
    assert check(allowed) is True


def test_sync_has_a_per_user_cooldown() -> None:
    cooldown = Operator.sync.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 60)
    assert Operator.sync._buckets.type is commands.BucketType.user


def test_setup_registers_the_operator_cog() -> None:
    bot = _bot()

    asyncio.run(ops_setup(bot))

    cog = bot.get_cog("Operator")
    assert isinstance(cog, Operator)
    assert cog.bot is bot


def test_sync_replies_in_the_operators_language(monkeypatch: pytest.MonkeyPatch) -> None:
    for synced, expected in (
        (True, "Anwendungsbefehle synchronisiert."),
        (False, "Die Anwendungsbefehle sind bereits aktuell."),
    ):
        bot = _bot()

        async def fake_sync_commands(synced: bool = synced) -> list[SyncResult]:
            return _results(synced=synced)

        monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
        ctx = StubContext()
        ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)

        _run(Operator(bot), ctx)

        assert ctx.replies[0][1]["embed"].description == expected
