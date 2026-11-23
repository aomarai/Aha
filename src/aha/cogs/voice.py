"""``/join``, ``/leave`` and ``/play``: the bot in a voice channel (ADR 0002, issues #169, #170).

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
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.i18n import DEFAULT_LOCALE, locale_of, t
from aha.replies import reply
from aha.textutil import clip, plain
from aha.voice import AudioBackend, AudioEvent, Track, VoiceError
from aha.youtube import LinkError, video_id, watch_url

__all__ = ["SWEEP_INTERVAL", "Voice", "format_length", "setup"]

log = logging.getLogger(__name__)

#: Seconds between looks at who is still listening.
SWEEP_INTERVAL = 30.0

#: The task that ends idle sessions.
JANITOR_TASK = "voice-janitor"


def format_length(milliseconds: int) -> str:
    """``3:33`` or ``1:02:03`` for a track length."""
    hours, rest = divmod(milliseconds // 1000, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


class Voice(commands.Cog):
    """Join and leave voice channels, and leave by itself when nobody is listening."""

    def __init__(self, bot: AhaBot, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.bot = bot
        self._clock = clock
        #: When each guild's session became idle (silent or alone); in memory, because a restart
        #: drops every session anyway.
        self._idle_since: dict[int, float] = {}
        #: The text channel of each guild's latest ``/play``, where a playback problem is reported.
        self._announce: dict[int, int] = {}

    async def cog_load(self) -> None:
        """Start the janitor (it does nothing until a session exists) and hear playback problems."""
        if JANITOR_TASK not in self.bot.tasks:
            self.bot.tasks.spawn(self._janitor, name=JANITOR_TASK, restart=True)
            if self.bot.audio is not None:
                self.bot.audio.subscribe(self._on_audio_event)

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

    async def _precheck(
        self,
        ctx: commands.Context[commands.Bot],
        guild: discord.Guild,
        audio: AudioBackend,
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> str | None:
        """May the bot go to ``channel``? ``"same"`` when it is already there, ``"ok"`` when it
        may move or join, ``None`` after replying with the reason it may not."""
        locale = locale_of(ctx)
        permissions = channel.permissions_for(guild.me) if guild.me else None
        if permissions is not None and not (permissions.connect and permissions.speak):
            message = t("voice.missing_permissions", locale, channel=plain(channel.name))
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return None
        current = audio.channel_id(guild.id)
        if current == channel.id:
            return "same"
        if current is None and audio.player_count() >= self.bot.settings.voice_max_players:
            await reply(ctx, embed=embeds.error(t("voice.busy", locale)), ephemeral=True)
            return None
        if current is not None and audio.is_playing(guild.id) and not self._may_manage(ctx):
            await reply(ctx, embed=embeds.error(t("voice.in_use", locale)), ephemeral=True)
            return None
        return "ok"

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
        state = await self._precheck(ctx, ctx.guild, audio, channel)
        if state is None:
            return
        if state == "same":
            message = t("voice.already_here", locale, channel=plain(channel.name))
            await reply(ctx, embed=embeds.info(message), ephemeral=True)
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
        name="play",
        description="Play a YouTube link in your voice channel.",
        extras={"category": "Fun"},
    )
    @app_commands.describe(link="A YouTube video link.")
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def play(self, ctx: commands.Context[commands.Bot], link: str) -> None:
        """Play one YouTube video in the invoker's voice channel."""
        audio = await self._require_audio(ctx)
        if audio is None or ctx.guild is None:
            return
        locale = locale_of(ctx)
        outcome = self.bot.metrics.voice_tracks
        try:
            video = video_id(link)
        except LinkError as error:
            outcome.labels(outcome="refused").inc()
            await reply(ctx, embed=embeds.error(t(error.key, locale)), ephemeral=True)
            return
        channel = self._caller_channel(ctx)
        if channel is None:
            await reply(ctx, embed=embeds.error(t("voice.join_first", locale)), ephemeral=True)
            return
        state = await self._precheck(ctx, ctx.guild, audio, channel)
        if state is None:
            return
        if audio.is_playing(ctx.guild.id):
            outcome.labels(outcome="refused").inc()
            message = t("voice.already_playing", locale)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        await ctx.defer()
        try:
            track = await audio.load(video)
            refusal = self._refusal(track, locale)
            if refusal is not None:
                outcome.labels(outcome="refused").inc()
                await reply(ctx, embed=embeds.error(refusal), ephemeral=True)
                return
            if state == "ok":
                await audio.join(channel)
            await audio.play(ctx.guild.id, track)
        except VoiceError as error:
            outcome.labels(outcome="failed").inc()
            await reply(ctx, embed=embeds.error(t(error.key, locale)), ephemeral=True)
            return
        self._idle_since.pop(ctx.guild.id, None)
        self._announce[ctx.guild.id] = ctx.channel.id
        outcome.labels(outcome="ok").inc()
        message = t(
            "voice.now_playing",
            locale,
            title=self._link(track),
            length=format_length(track.length_ms),
            who=plain(ctx.author.display_name),
        )
        await reply(ctx, embed=embeds.success(message))

    def _refusal(self, track: Track, locale: str) -> str | None:
        """Why this track may not be played, or ``None``."""
        if track.is_stream:
            return t("voice.live_unsupported", locale)
        limit = self.bot.settings.voice_max_track_minutes
        if track.length_ms > limit * 60_000:
            return t("voice.too_long", locale, minutes=limit)
        return None

    @staticmethod
    def _link(track: Track) -> str:
        """The title as a link to the video. The title is untrusted: no brackets, so it cannot end
        the link early and point somewhere else."""
        title = clip(plain(track.title), 100).replace("[", "(").replace("]", ")")
        return f"[{title}]({watch_url(track.video_id)})"

    async def _on_audio_event(self, event: AudioEvent) -> None:
        """Tell the channel the last ``/play`` came from that the track did not play."""
        self.bot.metrics.voice_tracks.labels(outcome="failed").inc()
        channel_id = self._announce.get(event.guild_id)
        channel = None if channel_id is None else self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        guild = getattr(channel, "guild", None)
        locale = str(guild.preferred_locale.value) if guild is not None else DEFAULT_LOCALE
        try:
            await channel.send(
                t("voice.track_failed", locale), allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            log.info("Could not report a playback problem (%s)", type(exc).__name__)

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
