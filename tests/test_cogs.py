"""Offline tests for the command surface.

These exercise real cogs and command callbacks without a Discord connection:
no token, no network. Discord objects are replaced with minimal stubs.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from aha.cogs.meta import Meta
from aha.cogs.meta import setup as meta_setup
from tests.helpers import StubBot, StubContext, as_bot, as_context


def test_ping_reports_latency_in_milliseconds() -> None:
    bot = StubBot(latency=0.042)
    cog = Meta(as_bot(bot))
    ctx = StubContext()

    # The decorator wraps the function in a HybridCommand; call the original
    # callback directly (its decorated signature is not statically callable).
    callback = cast("Any", cog.ping.callback)
    asyncio.run(callback(cog, as_context(ctx)))

    assert ctx.replies == [("Pong! WebSocket latency: 42 ms", {})]


def test_setup_registers_the_meta_cog() -> None:
    bot = StubBot(latency=0.0)

    asyncio.run(meta_setup(as_bot(bot)))

    assert len(bot.added_cogs) == 1
    cog = bot.added_cogs[0]
    assert isinstance(cog, Meta)
    assert cast("Any", cog.bot) is bot
