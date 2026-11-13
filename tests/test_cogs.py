"""Offline tests for the command surface (access control included)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from discord.utils import maybe_coroutine

from aha.bot import AhaBot
from aha.cogs.meta import Meta
from aha.cogs.meta import setup as meta_setup
from aha.config import Settings
from tests.helpers import Named, StubContext, as_context


def _bot() -> AhaBot:
    return AhaBot(Settings(token="test-token"))


def _run(cog: Meta, ctx: StubContext, method: str) -> None:
    callback = cast("Any", getattr(cog, method).callback)
    asyncio.run(callback(cog, as_context(ctx)))


async def _run_checks(ctx: Any) -> list[Any]:
    return [await maybe_coroutine(check, ctx) for check in Meta.sync.checks]


def _ctx_with_permissions(manage_guild: bool) -> StubContext:
    ctx = StubContext()
    # ``commands.has_permissions`` reads the cached ``Context.permissions``.
    ctx.permissions = discord.Permissions(manage_guild=manage_guild)
    return ctx


def test_ping_reports_latency_in_milliseconds(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "latency", property(lambda self: 0.042))
    cog = Meta(bot)
    ctx = StubContext()

    _run(cog, ctx, "ping")

    content, kwargs = ctx.replies[0]
    assert content is None
    assert "embed" in kwargs
    embed = kwargs["embed"]
    assert embed.description == "WebSocket latency: 42 ms"


def test_ping_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    monkeypatch.setattr(type(bot), "latency", property(lambda self: 0.042))
    cog = Meta(bot)
    ctx = StubContext(reject_embeds=True)

    _run(cog, ctx, "ping")

    assert ctx.replies == [("WebSocket latency: 42 ms", {})]


def test_ping_has_a_per_user_cooldown() -> None:
    cooldown = Meta.ping.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 5)
    # discord.py exposes the bucket type only through the mapping.
    assert Meta.ping._buckets.type is commands.BucketType.user


def test_sync_delegates_to_the_bot_and_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    synced: list[str] = []

    async def fake_sync_commands() -> None:
        synced.append("sync")

    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
    cog = Meta(bot)
    ctx = StubContext()

    _run(cog, ctx, "sync")

    assert synced == ["sync"]
    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.description == "Application commands synced."
    assert embed.color is not None
    assert embed.color.value == 0x57F287  # success green


def test_sync_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()

    async def fake_sync_commands() -> None:
        return None

    monkeypatch.setattr(bot, "sync_commands", fake_sync_commands)
    cog = Meta(bot)
    ctx = StubContext(reject_embeds=True)

    _run(cog, ctx, "sync")

    assert ctx.replies == [("Application commands synced.", {})]


def test_sync_is_guild_only_and_requires_manage_guild() -> None:
    app_command = Meta.sync.app_command
    assert app_command is not None
    assert app_command.guild_only is True
    assert app_command.default_permissions is not None
    assert app_command.default_permissions.manage_guild is True


def test_command_metadata_matches_the_public_surface() -> None:
    assert Meta.ping.qualified_name == "ping"
    assert Meta.ping.description == "Check the bot's WebSocket latency."
    assert Meta.sync.qualified_name == "sync"
    assert Meta.sync.description == "Re-sync the slash commands (requires Manage Server)."


def test_sync_checks_reject_a_member_without_manage_guild() -> None:
    with pytest.raises(commands.MissingPermissions) as caught:
        asyncio.run(_run_checks(_ctx_with_permissions(manage_guild=False)))
    assert caught.value.missing_permissions == ["manage_guild"]


def test_sync_checks_accept_a_member_with_manage_guild() -> None:
    results = asyncio.run(_run_checks(_ctx_with_permissions(manage_guild=True)))
    assert results == [True, True]


def test_sync_checks_reject_direct_messages() -> None:
    ctx = _ctx_with_permissions(manage_guild=True)
    ctx.guild = None
    with pytest.raises(commands.NoPrivateMessage):
        asyncio.run(_run_checks(ctx))


def test_sync_app_check_requires_manage_guild() -> None:
    app_command = Meta.sync.app_command
    assert app_command is not None
    (check,) = app_command.checks
    denied = cast("Any", Named(permissions=discord.Permissions.none()))
    with pytest.raises(app_commands.MissingPermissions) as caught:
        check(denied)
    assert caught.value.missing_permissions == ["manage_guild"]
    allowed = cast("Any", Named(permissions=discord.Permissions(manage_guild=True)))
    assert check(allowed) is True


def test_setup_registers_the_meta_cog() -> None:
    bot = _bot()

    asyncio.run(meta_setup(bot))

    cog = bot.get_cog("Meta")
    assert isinstance(cog, Meta)
    assert cog.bot is bot
