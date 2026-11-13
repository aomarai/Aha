"""Diagnostics and meta commands."""

from __future__ import annotations

import discord
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.replies import reply

__all__ = ["Meta", "setup"]


class Meta(commands.Cog):
    """Basic bot introspection."""

    def __init__(self, bot: AhaBot) -> None:
        self.bot = bot

    @hybrid_command(name="ping", description="Check the bot's WebSocket latency.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ping(self, ctx: commands.Context[commands.Bot]) -> None:
        """Reply with the current gateway latency in milliseconds."""
        latency_ms = round(self.bot.latency * 1000)
        text = f"WebSocket latency: {latency_ms} ms"
        try:
            await reply(ctx, embed=embeds.status(text))
        except discord.HTTPException:
            # The channel may lack embed permission; keep the reply alive.
            await reply(ctx, text)


async def setup(bot: AhaBot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Meta(bot))
