"""Operator-only commands, loaded only where they are meant to exist.

``/sync`` rewrites the application's command set, so it must not be reachable by every guild
admin (a Manage Server member of any guild could otherwise rewrite the commands every guild
sees). In production, syncing is the ``aha sync`` deploy Job and this cog is not loaded at all
(see :data:`aha.bot.OPERATOR_COG`); it is available only with ``DEV_GUILD_ID`` (a dev guild) or
``SYNC_ON_STARTUP`` (single-process self-hosting), and then only to the bot's owner.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.replies import reply

__all__ = ["Operator", "setup"]


class Operator(commands.Cog):
    """Commands only the bot's owner may run."""

    def __init__(self, bot: AhaBot) -> None:
        self.bot = bot

    @hybrid_command(
        name="sync",
        description="Re-sync the slash commands (bot owner, requires Manage Server).",
    )
    @commands.cooldown(1, 60, commands.BucketType.user)
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.is_owner()
    async def sync(self, ctx: commands.Context[commands.Bot]) -> None:
        """Re-sync the application command tree and report what changed.

        ``commands.guild_only`` also flags the app command as guild-only, and ``prepare``
        runs the ``commands.*`` checks on the slash path, so both surfaces are gated.
        """
        # A global sync can outlast Discord's 3-second acknowledgement window,
        # after which a reply would be rejected.
        await ctx.defer(ephemeral=True)
        results = await self.bot.sync_commands()
        if any(result.synced for result in results):
            text = "Application commands synced."
        else:
            text = "Application commands are already up to date."
        try:
            await reply(ctx, embed=embeds.success(text))
        except discord.HTTPException:
            await reply(ctx, text)


async def setup(bot: AhaBot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Operator(bot))
