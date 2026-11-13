"""Per-server settings: ``/config`` shows them, ``/configure`` changes them (#98).

Both need Manage Server, are guild-only and reply ephemerally. The permission check runs on the
server for both the slash and message surfaces (``commands.has_permissions`` runs in ``prepare``),
not only as Discord-side visibility. Every change writes one ``aha.audit`` record naming the
setting (never the value). A server that removes the bot has its settings and jobs deleted after
:data:`PURGE_DELAY`, unless the bot is back by then (#99).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.guildsettings import PROTECTED_COMMANDS, GuildSettingsCache
from aha.jobstore import Job, JobStore
from aha.replies import reply
from aha.textutil import code

__all__ = ["PURGE_DELAY", "PURGE_KIND", "Config", "setup"]

log = logging.getLogger(__name__)
audit = logging.getLogger("aha.audit")

#: ``scheduled_jobs.kind`` of the delayed deletion of a departed guild's data.
PURGE_KIND = "guild_purge"

#: How long after the bot leaves a guild its data is deleted (kept so a re-invite restores it).
PURGE_DELAY = timedelta(days=30)

_CHANNEL = re.compile(r"(?:<#)?(\d{1,20})>?")

_UNAVAILABLE = "Server settings are not available: no database is configured."


def _span(text: str) -> str:
    """Show ``text`` as inline code."""
    return f"`{code(text)}`"


def utcnow() -> datetime:
    """Return the current time in UTC (a seam for tests)."""
    return datetime.now(UTC)


class Config(commands.Cog):
    """View and change this server's settings, and clean up after a departed server."""

    def __init__(self, bot: AhaBot, store: JobStore | None = None) -> None:
        self.bot = bot
        self._jobs = store if store is not None else (JobStore(bot.db) if bot.db else None)

    def _settings(self) -> GuildSettingsCache | None:
        return self.bot.guild_settings

    @hybrid_command(
        name="config", description="Show this server's settings.", extras={"category": "Admin"}
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def config(self, ctx: commands.Context[commands.Bot]) -> None:
        """Show the settings in force for this server."""
        settings = self._settings()
        if settings is None or ctx.guild is None:
            await reply(ctx, embed=embeds.error(_UNAVAILABLE), ephemeral=True)
            return
        current = await settings.get(ctx.guild.id)
        channel = f"<#{current.log_channel_id}>" if current.log_channel_id else "not set"
        disabled = ", ".join(_span(name) for name in sorted(current.disabled_commands)) or "none"
        text = f"**Log channel:** {channel}\n**Disabled commands:** {disabled}"
        await reply(ctx, embed=embeds.info(text), ephemeral=True)

    @hybrid_command(
        name="configure",
        description="Change a server setting.",
        extras={"category": "Admin"},
    )
    @app_commands.describe(
        setting="What to change.",
        value="A channel for log-channel (or off), a command name for disable and enable.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.cooldown(1, 5, commands.BucketType.guild)
    async def configure(
        self,
        ctx: commands.Context[commands.Bot],
        setting: Literal["log-channel", "disable", "enable"],
        value: str,
    ) -> None:
        """Change one setting. Only members with Manage Server get this far."""
        settings = self._settings()
        if settings is None or ctx.guild is None:
            await reply(ctx, embed=embeds.error(_UNAVAILABLE), ephemeral=True)
            return
        guild, by, text = ctx.guild, ctx.author.id, value.strip()
        if setting == "log-channel":
            channel_id = self._parse_channel(guild, text)
            if channel_id is False:
                await reply(
                    ctx,
                    embed=embeds.error("Give a text channel in this server, or `off`."),
                    ephemeral=True,
                )
                return
            await settings.set_log_channel(guild.id, channel_id, by)
            done = "Log channel cleared." if channel_id is None else "Log channel set."
        else:
            name = text.lstrip("/").lower()
            if name in PROTECTED_COMMANDS:
                await reply(
                    ctx,
                    embed=embeds.error(f"{_span(name)} can't be disabled."),
                    ephemeral=True,
                )
                return
            if ctx.bot.get_command(name) is None:
                await reply(
                    ctx, embed=embeds.error(f"There is no command {_span(name)}."), ephemeral=True
                )
                return
            if setting == "disable":
                await settings.disable(guild.id, name, by)
                done = f"{_span(name)} is now disabled in this server."
            else:
                await settings.enable(guild.id, name, by)
                done = f"{_span(name)} is enabled in this server."
        audit.info("Server setting changed", extra={"event": "config.changed", "setting": setting})
        await reply(ctx, embed=embeds.success(done), ephemeral=True)

    @staticmethod
    def _parse_channel(guild: discord.Guild, text: str) -> int | Literal[False] | None:
        """A text channel id from a mention or id; ``None`` for ``off``; ``False`` if invalid."""
        if text.lower() == "off":
            return None
        match = _CHANNEL.fullmatch(text)
        if match is None:
            return False
        channel = guild.get_channel(int(match.group(1)))
        return channel.id if isinstance(channel, discord.abc.Messageable) else False

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """Schedule the deletion of a departed guild's data (kept for a re-invite)."""
        if self._jobs is None:
            return
        await self._jobs.enqueue(PURGE_KIND, utcnow() + PURGE_DELAY, guild_id=guild.id)
        log.info("Left guild %s: its data is scheduled for deletion", guild.id)

    async def purge_guild(self, job: Job) -> None:
        """Delete a departed guild's settings and jobs, unless the bot has rejoined it."""
        guild_id = job.guild_id
        if guild_id is None or self._jobs is None or self.bot.get_guild(guild_id) is not None:
            return
        settings = self._settings()
        if settings is not None:
            await settings.delete(guild_id)
        await self._jobs.purge_guild(guild_id)
        log.info("Deleted the data of departed guild %s", guild_id)


async def setup(bot: AhaBot) -> None:
    """Add the cog and register the purge handler when a scheduler exists."""
    cog = Config(bot)
    await bot.add_cog(cog)
    if bot.scheduler is not None:
        bot.scheduler.register(PURGE_KIND, cog.purge_guild)
