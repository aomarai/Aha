"""``/join`` and ``/leave``: the bot in a voice channel (ADR 0002, issue #169).

Audio runs in a Lavalink node; this cog only decides whether the bot may join, and when it should
leave. Everything is per guild and lives on the pod that owns the guild's shard, so nothing here is
shared between pods and nothing may call it from a scheduled job (ADR 0001 section 2 and 5).

Without ``LAVALINK_URL`` the commands exist (the command tree must be the same on every process)
but say that playback is off. The bot never listens: it joins deafened, and it needs only the
``voice_states`` intent, which is not privileged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import discord
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.i18n import locale_of, t
from aha.replies import reply
from aha.textutil import plain
from aha.voice import AudioBackend, VoiceError

__all__ = ["SWEEP_INTERVAL", "Voice", "setup"]

log = logging.getLogger(__name__)

#: Seconds between looks at who is still listening.
SWEEP_INTERVAL = 30.0

#: The task that ends idle sessions.
JANITOR_TASK = "voice-janitor"


class Voice(commands.Cog):
    """Join and leave voice channels, and leave by itself when nobody is listening."""

    def __init__(self, bot: AhaBot, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.bot = bot
        self._clock = clock
        #: When each guild's session became idle (silent or alone); in memory, because a restart
        #: drops every session anyway.
        self._idle_since: dict[int, float] = {}

    async def cog_load(self) -> None:
        """Start the janitor (it does nothing until a session exists)."""
        if JANITOR_TASK not in self.bot.tasks:
            self.bot.tasks.spawn(self._janitor, name=JANITOR_TASK, restart=True)

    # -- helpers ---------------------------------------------------------------------------------

    async def _require_audio(self, ctx: commands.Context[commands.Bot]) -> AudioBackend | None:
        """The backend, or tell the user playback is off or the node is down."""
        audio = self.bot.audio
        locale = locale_of(ctx)
        if audio is None:
            await reply(ctx, embed=embeds.error(t("voice.off", locale)), ephemeral=True)
            return None
        if not audio.up:
            await reply(ctx, embed=embeds.error(t("voice.node_down", locale)), ephemeral=True)
            return None
        return audio

    @staticmethod
    def _caller_channel(
        ctx: commands.Context[commands.Bot],
    ) -> discord.VoiceChannel | discord.StageChannel | None:
        """The voice channel the invoker is in (from the voice-state cache), if any."""
        member: Any = ctx.author
        state = getattr(member, "voice", None)
        channel = None if state is None else state.channel
        if isinstance(channel, discord.VoiceChannel | discord.StageChannel):
            return channel
        return None

    def listeners(self, channel_id: int) -> int:
        """How many others share the channel with the bot (0 when it is alone or gone)."""
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel | discord.StageChannel):
            return 0
        return max(0, len(channel.voice_states) - 1)

    # -- commands --------------------------------------------------------------------------------

    @hybrid_command(
        name="join",
        description="Join your voice channel.",
        extras={"category": "Fun"},
    )
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def join(self, ctx: commands.Context[commands.Bot]) -> None:
        """Join the invoker's voice channel, or move there."""
        audio = await self._require_audio(ctx)
        if audio is None or ctx.guild is None:
            return
        locale = locale_of(ctx)
        channel = self._caller_channel(ctx)
        if channel is None:
            await reply(ctx, embed=embeds.error(t("voice.join_first", locale)), ephemeral=True)
            return
        permissions = channel.permissions_for(ctx.guild.me) if ctx.guild.me else None
        if permissions is not None and not (permissions.connect and permissions.speak):
            message = t("voice.missing_permissions", locale, channel=plain(channel.name))
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        current = audio.channel_id(ctx.guild.id)
        if current == channel.id:
            message = t("voice.already_here", locale, channel=plain(channel.name))
            await reply(ctx, embed=embeds.info(message), ephemeral=True)
            return
        if current is None and audio.player_count() >= self.bot.settings.voice_max_players:
            await reply(ctx, embed=embeds.error(t("voice.busy", locale)), ephemeral=True)
            return
        if current is not None and audio.is_playing(ctx.guild.id) and not self._may_manage(ctx):
            message = t("voice.in_use", locale)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        await ctx.defer()
        try:
            await audio.join(channel)
        except VoiceError as error:
            await reply(ctx, embed=embeds.error(t(error.key, locale)), ephemeral=True)
            return
        self._idle_since.pop(ctx.guild.id, None)
        message = t("voice.joined", locale, channel=plain(channel.name))
        await reply(ctx, embed=embeds.success(message))

    @hybrid_command(
        name="leave",
        description="Leave the voice channel.",
        extras={"category": "Fun"},
    )
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def leave(self, ctx: commands.Context[commands.Bot]) -> None:
        """Leave the voice channel (from the same channel, or with Manage Channels)."""
        audio = self.bot.audio
        locale = locale_of(ctx)
        if audio is None or ctx.guild is None:
            await reply(ctx, embed=embeds.error(t("voice.off", locale)), ephemeral=True)
            return
        current = audio.channel_id(ctx.guild.id)
        if current is None:
            await reply(ctx, embed=embeds.error(t("voice.not_connected", locale)), ephemeral=True)
            return
        caller = self._caller_channel(ctx)
        if (caller is None or caller.id != current) and not self._may_manage(ctx):
            await reply(ctx, embed=embeds.error(t("voice.not_with_me", locale)), ephemeral=True)
            return
        await ctx.defer()
        await audio.leave(ctx.guild.id)
        self._idle_since.pop(ctx.guild.id, None)
        await reply(ctx, embed=embeds.success(t("voice.left", locale)))

    @staticmethod
    def _may_manage(ctx: commands.Context[commands.Bot]) -> bool:
        """Whether the invoker holds Manage Channels (and so may move or stop the bot)."""
        permissions = getattr(ctx.author, "guild_permissions", None)
        return bool(permissions is not None and permissions.manage_channels)

    # -- leaving by itself -----------------------------------------------------------------------

    async def _janitor(self) -> None:
        """Look at every session every :data:`SWEEP_INTERVAL` seconds, until cancelled."""
        while True:
            await self.sweep()
            await asyncio.sleep(SWEEP_INTERVAL)

    async def sweep(self) -> list[int]:
        """Leave the guilds whose session has been silent or alone for the idle time.

        Returns the guild ids it left.
        """
        audio = self.bot.audio
        if audio is None:
            return []
        now = self._clock()
        idle_limit = self.bot.settings.voice_idle_minutes * 60.0
        present = set(audio.guild_ids())
        for gone in set(self._idle_since) - present:
            del self._idle_since[gone]
        left: list[int] = []
        for guild_id in sorted(present):
            channel_id = audio.channel_id(guild_id)
            alone = channel_id is None or self.listeners(channel_id) == 0
            if audio.is_playing(guild_id) and not alone:
                self._idle_since.pop(guild_id, None)
                continue
            since = self._idle_since.setdefault(guild_id, now)
            if now - since >= idle_limit:
                await audio.leave(guild_id)
                del self._idle_since[guild_id]
                left.append(guild_id)
                log.info(
                    "Left the voice channel of guild %s after %d idle minutes",
                    guild_id,
                    self.bot.settings.voice_idle_minutes,
                )
        return left


async def setup(bot: AhaBot) -> None:
    """Add the cog."""
    await bot.add_cog(Voice(bot))
