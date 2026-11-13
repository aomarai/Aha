"""Offline tests for the command surface (access control included)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from discord.ext import commands

from aha.bot import AhaBot
from aha.cogs.meta import Meta
from aha.cogs.meta import setup as meta_setup
from aha.config import Settings
from tests.helpers import StubContext, as_context, assert_reply


def _bot() -> AhaBot:
    return AhaBot(Settings(token="test-token"))


def _run(cog: Meta, ctx: StubContext, method: str) -> None:
    callback = cast("Any", getattr(cog, method).callback)
    asyncio.run(callback(cog, as_context(ctx)))


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

    assert_reply(ctx.replies, "WebSocket latency: 42 ms")


def test_ping_has_a_per_user_cooldown() -> None:
    cooldown = Meta.ping.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 5)
    # discord.py exposes the bucket type only through the mapping.
    assert Meta.ping._buckets.type is commands.BucketType.user


def test_command_metadata_matches_the_public_surface() -> None:
    assert Meta.ping.qualified_name == "ping"
    assert Meta.ping.description == "Check the bot's WebSocket latency."


def test_setup_registers_the_meta_cog() -> None:
    bot = _bot()

    asyncio.run(meta_setup(bot))

    cog = bot.get_cog("Meta")
    assert isinstance(cog, Meta)
    assert cog.bot is bot
