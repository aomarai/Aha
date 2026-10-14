"""Offline tests for the command surface.

These exercise real cogs and command callbacks without a Discord connection:
no token, no network. Discord objects are replaced with minimal stubs.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from discord.ext import commands

from aha.cogs.meta import Meta
from aha.cogs.meta import setup as meta_setup


class _StubBot:
    """Minimal stand-in exposing only what the cog under test uses."""

    def __init__(self, latency: float) -> None:
        self.latency = latency
        self.added_cogs: list[commands.Cog] = []

    async def add_cog(self, cog: commands.Cog) -> None:
        self.added_cogs.append(cog)


class _StubContext:
    """Records replies instead of talking to Discord."""

    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, content: str, **_: Any) -> None:
        self.replies.append(content)


def _as_bot(stub: _StubBot) -> commands.Bot:
    return cast("commands.Bot", stub)


def _as_context(stub: _StubContext) -> commands.Context[commands.Bot]:
    return cast("commands.Context[commands.Bot]", stub)


def test_ping_reports_latency_in_milliseconds() -> None:
    bot = _StubBot(latency=0.042)
    cog = Meta(_as_bot(bot))
    ctx = _StubContext()

    # The decorator wraps the function in a HybridCommand; call the original
    # callback directly (its decorated signature is not statically callable).
    callback = cast("Any", cog.ping.callback)
    asyncio.run(callback(cog, _as_context(ctx)))

    assert ctx.replies == ["Pong! WebSocket latency: 42 ms"]


def test_setup_registers_the_meta_cog() -> None:
    bot = _StubBot(latency=0.0)

    asyncio.run(meta_setup(_as_bot(bot)))

    assert len(bot.added_cogs) == 1
    assert isinstance(bot.added_cogs[0], Meta)
