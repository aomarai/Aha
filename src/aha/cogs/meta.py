"""Diagnostics and meta commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command

__all__ = ["Meta", "setup"]


class Meta(commands.Cog):
    """Basic bot introspection and operator commands."""

    def __init__(self, bot: AhaBot) -> None:
        self.bot = bot

    @hybrid_command(name="ping", description="Check the bot's WebSocket latency.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ping(self, ctx: commands.Context[commands.Bot]) -> None:
        """Reply with the current gateway latency in milliseconds."""
        latency_ms = round(self.bot.latency * 1000)
        text = f"WebSocket latency: {latency_ms} ms"
        try:
            await ctx.reply(embed=embeds.status(text))
        except discord.HTTPException:
            # The channel may lack embed permission; keep the reply alive.
            await ctx.reply(text)

    @hybrid_command(
        name="sync",
        description="Re-sync the slash commands (requires Manage Server).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def sync(self, ctx: commands.Context[commands.Bot]) -> None:
        """Re-sync the application command tree and report success.

        ``commands.guild_only`` also flags the app command as guild-only, and
        ``prepare`` runs the ``commands.*`` checks on the slash path, so both
        surfaces are gated.
        """
        # A global sync can outlast Discord's 3-second acknowledgement window,
        # after which a reply would be rejected.
        await ctx.defer(ephemeral=True)
        await self.bot.sync_commands()
        text = "Application commands synced."
        try:
            await ctx.reply(embed=embeds.success(text))
        except discord.HTTPException:
            await ctx.reply(text)


async def setup(bot: AhaBot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Meta(bot))
