"""The voice queue's commands and buttons (#171, ADR 0002): ``/queue``, ``/nowplaying``, ``/skip``,
``/pause``, ``/resume``, ``/stop`` and ``/remove``, and the buttons under a now-playing message.

Who may do what: anyone in the bot's voice channel may pause and resume; only the person who queued
a track (or someone with Manage Channels) may skip it, remove it, or stop everything. Anyone may
read the queue. The buttons carry their whole state in the ``custom_id`` and are checked again at
every click with the same rules, so they keep working after a restart and for the right people only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, ClassVar

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds, logctx
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.i18n import DEFAULT_LOCALE, locale_of, t
from aha.playback import PlaybackQueue
from aha.queuestore import QueueEntry
from aha.replies import reply, respond
from aha.textutil import pack
from aha.voice import AudioBackend, VoiceError
from aha.voicechecks import (
    caller_channel,
    may_manage,
    precheck,
    require_audio,
    title_link,
)

__all__ = [
    "PAGE_LIMIT",
    "ControlButton",
    "Playback",
    "Result",
    "controls",
    "format_length",
    "setup",
]

log = logging.getLogger(__name__)

#: Longest ``/queue`` page (an embed description holds 4096).
PAGE_LIMIT = 3800

#: The actions a button can carry.
ACTIONS = ("pause", "skip", "stop")


def format_length(milliseconds: int) -> str:
    """``3:33`` or ``1:02:03`` for a track length."""
    hours, rest = divmod(milliseconds // 1000, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


@dataclass(frozen=True, slots=True)
class Result:
    """What to tell the user, and whether it worked."""

    ok: bool
    text: str


class ControlButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"aha:voice:(?P<action>pause|skip|stop):(?P<guild>\d+)",
):
    """Pause/resume, skip and stop under a now-playing message.

    The ``custom_id`` is ``aha:voice:<action>:<guild id>``.
    """

    _STYLES: ClassVar[dict[str, discord.ButtonStyle]] = {
        "pause": discord.ButtonStyle.secondary,
        "skip": discord.ButtonStyle.primary,
        "stop": discord.ButtonStyle.danger,
    }

    def __init__(self, action: str, guild_id: int, locale: str = DEFAULT_LOCALE) -> None:
        super().__init__(
            discord.ui.Button(
                label=t(f"voice.button_{action}", locale),
                style=self._STYLES[action],
                custom_id=f"aha:voice:{action}:{guild_id}",
            )
        )
        self.action = action
        self.guild_id = guild_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> ControlButton:
        """Rebuild the button from a ``custom_id`` that matched the template."""
        return cls(match["action"], int(match["guild"]))

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        """Run the action for whoever pressed it, if the rules allow them."""
        logctx.bind_interaction(interaction)
        locale = locale_of(interaction)
        cog = interaction.client.get_cog("Playback")
        if not isinstance(cog, Playback) or interaction.guild_id != self.guild_id:
            await respond(interaction, embed=embeds.error(t("voice.off", locale)), ephemeral=True)
            return
        result = await cog.press(interaction.user, self.guild_id, self.action, locale)
        card = embeds.success if result.ok else embeds.error
        await respond(interaction, embed=card(result.text), ephemeral=True)


def controls(guild_id: int, locale: str) -> discord.ui.View:
    """The three buttons for a guild's now-playing message."""
    view = discord.ui.View(timeout=None)
    for action in ACTIONS:
        view.add_item(ControlButton(action, guild_id, locale))
    return view


class Playback(commands.Cog):
    """Look at the queue, skip, pause, resume, stop and remove tracks."""

    def __init__(self, bot: AhaBot) -> None:
        self.bot = bot

    # -- the rules, shared by commands and buttons -----------------------------------------------

    def _services(self, locale: str) -> tuple[AudioBackend, PlaybackQueue] | Result:
        audio, playback = self.bot.audio, self.bot.playback
        if audio is None:
            return Result(False, t("voice.off", locale))
        if playback is None:
            return Result(False, t("voice.needs_database", locale))
        if not audio.up:
            return Result(False, t("voice.node_down", locale))
        return audio, playback

    @staticmethod
    def _with_me(audio: AudioBackend, member: Any, guild_id: int, locale: str) -> Result | None:
        """``None`` when the member may control this session, else why not."""
        current = audio.channel_id(guild_id)
        if current is None:
            return Result(False, t("voice.not_connected", locale))
        channel = caller_channel(member)
        if (channel is None or channel.id != current) and not may_manage(member):
            return Result(False, t("voice.not_with_me", locale))
        return None

    @staticmethod
    def _may_change(member: Any, entry: QueueEntry) -> bool:
        """Whether a member may skip, remove or stop this track: its requester, or a manager."""
        return entry.requester_id == getattr(member, "id", None) or may_manage(member)

    async def _controlled(
        self, member: Any, guild_id: int, locale: str
    ) -> tuple[AudioBackend, PlaybackQueue, list[QueueEntry]] | Result:
        """The services and the queue, once the member is allowed to control this session."""
        services = self._services(locale)
        if isinstance(services, Result):
            return services
        audio, playback = services
        refusal = self._with_me(audio, member, guild_id, locale)
        if refusal is not None:
            return refusal
        entries = await playback.entries(guild_id)
        if not entries or not audio.is_playing(guild_id):
            return Result(False, t("voice.nothing_playing", locale))
        return audio, playback, entries

    async def set_paused(
        self, member: Any, guild_id: int, locale: str, *, paused: bool | None
    ) -> Result:
        """Pause, resume, or (``None``) toggle."""
        found = await self._controlled(member, guild_id, locale)
        if isinstance(found, Result):
            return found
        audio, _, _ = found
        target = (not audio.is_paused(guild_id)) if paused is None else paused
        try:
            await audio.set_paused(guild_id, target)
        except VoiceError as error:
            return Result(False, t(error.key, locale))
        return Result(True, t("voice.paused" if target else "voice.resumed", locale))

    async def skip(self, member: Any, guild_id: int, locale: str) -> Result:
        """Skip the playing track (its requester, or a manager)."""
        found = await self._controlled(member, guild_id, locale)
        if isinstance(found, Result):
            return found
        _, playback, entries = found
        if not self._may_change(member, entries[0]):
            return Result(False, t("voice.not_yours", locale))
        started = await playback.skip(guild_id)
        if started is None:
            return Result(True, t("voice.skipped_empty", locale))
        link = title_link(started.title, started.video_id)
        return Result(True, t("voice.skipped_next", locale, title=link))

    async def stop(self, member: Any, guild_id: int, locale: str) -> Result:
        """Clear the queue and leave (the playing track's requester, or a manager)."""
        found = await self._controlled(member, guild_id, locale)
        if isinstance(found, Result):
            return found
        _, playback, entries = found
        if not self._may_change(member, entries[0]):
            return Result(False, t("voice.not_yours", locale))
        dropped = await playback.stop(guild_id)
        return Result(True, t("voice.stopped", locale, count=dropped))

    async def press(self, member: Any, guild_id: int, action: str, locale: str) -> Result:
        """A button was pressed."""
        if action == "pause":
            return await self.set_paused(member, guild_id, locale, paused=None)
        if action == "skip":
            return await self.skip(member, guild_id, locale)
        return await self.stop(member, guild_id, locale)

    # -- commands --------------------------------------------------------------------------------

    @staticmethod
    async def _say(ctx: commands.Context[commands.Bot], result: Result) -> None:
        card = embeds.success if result.ok else embeds.error
        await reply(ctx, embed=card(result.text), ephemeral=not result.ok)

    @hybrid_command(
        name="skip", description="Skip the track that is playing.", extras={"category": "Fun"}
    )
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def skip_command(self, ctx: commands.Context[commands.Bot]) -> None:
        """Skip the current track."""
        if ctx.guild is not None:
            await self._say(ctx, await self.skip(ctx.author, ctx.guild.id, locale_of(ctx)))

    @hybrid_command(name="pause", description="Pause the music.", extras={"category": "Fun"})
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def pause_command(self, ctx: commands.Context[commands.Bot]) -> None:
        """Pause playback."""
        if ctx.guild is not None:
            result = await self.set_paused(ctx.author, ctx.guild.id, locale_of(ctx), paused=True)
            await self._say(ctx, result)

    @hybrid_command(
        name="stop",
        description="Stop the music, clear the queue and leave.",
        extras={"category": "Fun"},
    )
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def stop_command(self, ctx: commands.Context[commands.Bot]) -> None:
        """Clear the queue and leave."""
        if ctx.guild is not None:
            await self._say(ctx, await self.stop(ctx.author, ctx.guild.id, locale_of(ctx)))

    @hybrid_command(
        name="resume",
        description="Resume the music, or start the saved queue again.",
        extras={"category": "Fun"},
    )
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def resume_command(self, ctx: commands.Context[commands.Bot]) -> None:
        """Resume a paused track; after a restart, rejoin and start the saved queue."""
        audio = await require_audio(self.bot, ctx)
        if audio is None or ctx.guild is None:
            return
        locale = locale_of(ctx)
        playback = self.bot.playback
        if playback is None:
            await self._say(ctx, Result(False, t("voice.needs_database", locale)))
            return
        guild_id = ctx.guild.id
        if audio.is_playing(guild_id):
            result = await self.set_paused(ctx.author, guild_id, locale, paused=False)
            await self._say(ctx, result)
            return
        if not await playback.entries(guild_id):
            await self._say(ctx, Result(False, t("voice.nothing_to_resume", locale)))
            return
        channel = caller_channel(ctx.author)
        if channel is None:
            await self._say(ctx, Result(False, t("voice.join_first", locale)))
            return
        state = await precheck(self.bot, ctx, ctx.guild, audio, channel)
        if state is None:
            return
        await ctx.defer()
        try:
            if state == "ok":
                await audio.join(channel)
            started = await playback.ensure_playing(guild_id)
        except VoiceError as error:
            await self._say(ctx, Result(False, t(error.key, locale)))
            return
        if started is None:
            await self._say(ctx, Result(False, t("voice.nothing_to_resume", locale)))
            return
        message = t(
            "voice.now_playing",
            locale,
            title=title_link(started.title, started.video_id),
            length=format_length(started.length_ms),
            who=f"<@{started.requester_id}>",
        )
        await reply(ctx, embed=embeds.success(message), view=controls(guild_id, locale))

    @hybrid_command(
        name="queue",
        description="Show what is playing and what is queued.",
        extras={"category": "Fun"},
    )
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def queue_command(self, ctx: commands.Context[commands.Bot]) -> None:
        """List the guild's queue (private to the invoker)."""
        locale = locale_of(ctx)
        playback = self.bot.playback
        if playback is None or ctx.guild is None:
            await self._say(ctx, Result(False, t("voice.needs_database", locale)))
            return
        entries = await playback.entries(ctx.guild.id)
        if not entries:
            await reply(ctx, embed=embeds.info(t("voice.queue_empty", locale)), ephemeral=True)
            return
        lines = [
            f"{'▶️' if number == 1 else f'{number}.'} "
            f"{title_link(entry.title, entry.video_id)} ({format_length(entry.length_ms)})"
            f" · <@{entry.requester_id}>"
            for number, entry in enumerate(entries, start=1)
        ]
        pages = pack(lines, PAGE_LIMIT)
        await reply(ctx, embeds=[embeds.info(page) for page in pages], ephemeral=True)

    @hybrid_command(
        name="nowplaying", description="Show the track that is playing.", extras={"category": "Fun"}
    )
    @commands.guild_only()
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def nowplaying_command(self, ctx: commands.Context[commands.Bot]) -> None:
        """Say what is playing, how far in, and who asked for it."""
        locale = locale_of(ctx)
        audio, playback = self.bot.audio, self.bot.playback
        if audio is None or playback is None or ctx.guild is None:
            await self._say(ctx, Result(False, t("voice.off", locale)))
            return
        entries = await playback.entries(ctx.guild.id)
        if not entries or not audio.is_playing(ctx.guild.id):
            await reply(ctx, embed=embeds.info(t("voice.nothing_playing", locale)), ephemeral=True)
            return
        head = entries[0]
        message = t(
            "voice.now_playing_line",
            locale,
            title=title_link(head.title, head.video_id),
            position=format_length(audio.position_ms(ctx.guild.id)),
            length=format_length(head.length_ms),
            who=f"<@{head.requester_id}>",
            state=t(
                "voice.state_paused" if audio.is_paused(ctx.guild.id) else "voice.state_playing",
                locale,
            ),
        )
        await reply(ctx, embed=embeds.info(message), ephemeral=True)

    @hybrid_command(
        name="remove", description="Remove a track from the queue.", extras={"category": "Fun"}
    )
    @app_commands.describe(position="Its number in /queue (the first is playing: use /skip).")
    @commands.guild_only()
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def remove_command(self, ctx: commands.Context[commands.Bot], position: int) -> None:
        """Remove one waiting track (its requester, or a manager)."""
        if ctx.guild is None:
            return
        locale = locale_of(ctx)
        guild_id = ctx.guild.id
        found = await self._controlled(ctx.author, guild_id, locale)
        if isinstance(found, Result):
            await self._say(ctx, found)
            return
        _, playback, entries = found
        if not 2 <= position <= len(entries):
            await self._say(ctx, Result(False, t("voice.bad_position", locale)))
            return
        target = entries[position - 1]
        if not self._may_change(ctx.author, target):
            await self._say(ctx, Result(False, t("voice.not_yours", locale)))
            return
        removed = await playback.remove(guild_id, target.id)
        if removed is None:  # it started or was removed in the meantime
            await self._say(ctx, Result(False, t("voice.bad_position", locale)))
            return
        link = title_link(removed.title, removed.video_id)
        await self._say(ctx, Result(True, t("voice.removed", locale, title=link)))


async def setup(bot: AhaBot) -> None:
    """Add the cog and register the persistent buttons."""
    bot.add_dynamic_items(ControlButton)
    await bot.add_cog(Playback(bot))
