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

from aha.commands import BotLike
from aha.guildsettings import CommandDisabled
from aha.i18n import DEFAULT_LOCALE, locale_of, t
from aha.replies import reply, respond

__all__ = ["WRAPPERS", "Errors", "setup"]

log = logging.getLogger(__name__)

#: Wrapper exceptions whose ``original`` attribute holds the real failure.
WRAPPERS = (
    commands.CommandInvokeError,
    commands.HybridCommandError,
    app_commands.CommandInvokeError,
)

_Respond = Callable[[str], Awaitable[None]]


def _join(names: list[str]) -> str:
    return ", ".join(name.replace("_", " ") for name in names)


def _describe(error: Exception, locale: str = DEFAULT_LOCALE) -> str | None:
    """Return a user-facing message for an expected error, else ``None``.

    Specific types are tested before ``CheckFailure`` since they subclass it.
    """
    if isinstance(error, (commands.BotMissingPermissions, app_commands.BotMissingPermissions)):
        return t(
            "error.bot_missing_permissions", locale, permissions=_join(error.missing_permissions)
        )
    if isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
        return t("error.missing_permissions", locale, permissions=_join(error.missing_permissions))
    if isinstance(error, (commands.MissingRole, app_commands.MissingRole)):
        return t("error.missing_role", locale)
    if isinstance(error, (commands.CommandOnCooldown, app_commands.CommandOnCooldown)):
        return t("error.cooldown", locale, seconds=f"{error.retry_after:.1f}")
    if isinstance(error, (commands.NoPrivateMessage, app_commands.NoPrivateMessage)):
        return t("error.no_private_message", locale)
    if isinstance(error, CommandDisabled):
        return t("error.command_disabled", locale)
    if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        return t("error.not_allowed", locale)
    return None


class Errors(commands.Cog):
    """Replies to failed commands and logs unexpected failures."""

    def __init__(self, bot: BotLike) -> None:
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

        async def send_error(message: str) -> None:
            # ``ephemeral`` only means anything for interaction-based contexts;
            # message replies (mention invocations) are public either way.
            if ctx.interaction is not None:
                await reply(ctx, message, ephemeral=True)
            else:
                await reply(ctx, message)

        await self._handle(
            error,
            command=ctx.command.qualified_name if ctx.command else None,
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=ctx.channel.id,
            user_id=ctx.author.id,
            locale=locale_of(ctx),
            respond=send_error,
        )

    @commands.Cog.listener("on_app_command_error")
    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Handle slash command failures forwarded by :class:`aha.bot.AhaTree`."""

        async def send_error(message: str) -> None:
            await respond(interaction, message, ephemeral=True)

        await self._handle(
            error,
            command=interaction.command.qualified_name if interaction.command else None,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            locale=locale_of(interaction),
            respond=send_error,
        )

    async def _handle(
        self,
        error: Exception,
        *,
        command: str | None,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        locale: str,
        respond: _Respond,
    ) -> None:
        while isinstance(error, WRAPPERS):
            error = error.original

        message = _describe(error, locale)
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
            message = t("error.unexpected", locale, error_id=error_id)

        try:
            await respond(message)
        except discord.HTTPException:
            log.warning("Could not deliver error reply for command %s", command, exc_info=True)


async def setup(bot: BotLike) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Errors(bot))
