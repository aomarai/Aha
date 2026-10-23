"""Global error handling for prefix, hybrid and slash commands.

Prefix and hybrid failures arrive through ``on_command_error``. Pure app-command
failures are forwarded by :class:`aha.bot.AhaTree` as an ``app_command_error``
event, so both surfaces end up in :meth:`Errors._handle`. Expected failures
(permissions, cooldowns, ...) get a short targeted reply; anything else is logged
with full context and answered with a generic message plus a correlation ID.
Tracebacks and other internals are never sent to users.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands

__all__ = ["Errors", "setup"]

log = logging.getLogger(__name__)

#: Wrapper exceptions whose ``original`` attribute holds the real failure.
_WRAPPERS = (
    commands.CommandInvokeError,
    commands.HybridCommandError,
    app_commands.CommandInvokeError,
)

_Respond = Callable[[str], Awaitable[None]]


def _join(names: list[str]) -> str:
    return ", ".join(name.replace("_", " ") for name in names)


def _describe(error: Exception) -> str | None:
    """Return a user-facing message for an expected error, else ``None``.

    Specific types are tested before ``CheckFailure`` since they subclass it.
    """
    if isinstance(error, (commands.BotMissingPermissions, app_commands.BotMissingPermissions)):
        return f"I'm missing permissions to do that: {_join(error.missing_permissions)}."
    if isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
        return f"You need these permissions to do that: {_join(error.missing_permissions)}."
    if isinstance(error, (commands.MissingRole, app_commands.MissingRole)):
        return "You don't have the role required to do that."
    if isinstance(error, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
        return f"That command is on cooldown. Try again in {error.retry_after:.1f}s."
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return "This command can't be used in direct messages."
    if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        return "You're not allowed to use that command."
    return None


class Errors(commands.Cog):
    """Replies to failed commands and logs unexpected failures."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener("on_command_error")
    async def on_command_error(
        self, ctx: commands.Context[commands.Bot], error: commands.CommandError
    ) -> None:
        """Handle prefix and hybrid command failures."""
        if isinstance(error, commands.CommandNotFound):
            # Any @mention that isn't a command lands here; stay quiet.
            log.debug("Ignoring unknown command from user %s", ctx.author.id)
            return

        async def respond(message: str) -> None:
            await ctx.reply(message, ephemeral=True)

        await self._handle(
            error,
            command=ctx.command.qualified_name if ctx.command else None,
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=ctx.channel.id,
            user_id=ctx.author.id,
            respond=respond,
        )

    @commands.Cog.listener("on_app_command_error")
    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Handle slash command failures forwarded by :class:`aha.bot.AhaTree`."""

        async def respond(message: str) -> None:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)

        await self._handle(
            error,
            command=interaction.command.qualified_name if interaction.command else None,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            respond=respond,
        )

    async def _handle(
        self,
        error: Exception,
        *,
        command: str | None,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        respond: _Respond,
    ) -> None:
        while isinstance(error, _WRAPPERS):
            error = error.original

        message = _describe(error)
        if message is None:
            error_id = uuid.uuid4().hex[:8]
            log.error(
                "Unexpected error in command %s (error_id=%s guild=%s channel=%s user=%s)",
                command,
                error_id,
                guild_id,
                channel_id,
                user_id,
                exc_info=error,
            )
            message = f"Something went wrong. If it keeps happening, quote error ID `{error_id}`."

        try:
            await respond(message)
        except discord.HTTPException:
            log.warning("Could not deliver error reply for command %s", command, exc_info=True)


async def setup(bot: commands.Bot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Errors(bot))
