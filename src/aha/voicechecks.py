"""Checks the voice commands share: is audio available, where is the caller, may the bot go there.

They live apart from the cogs because both the join/play cog and the queue-control cog need them,
and they take plain objects (a bot, a context, a member), not a cog, so a button click can use them
too.
"""

from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.i18n import locale_of, t
from aha.replies import reply
from aha.textutil import clip, plain
from aha.voice import AudioBackend
from aha.youtube import watch_url

__all__ = [
    "VocalChannel",
    "caller_channel",
    "may_manage",
    "precheck",
    "require_audio",
    "title_link",
]

#: A channel the bot can play audio in.
VocalChannel = discord.VoiceChannel | discord.StageChannel


def caller_channel(member: Any) -> VocalChannel | None:
    """The voice channel a member is in (from the voice-state cache), if any."""
    state = getattr(member, "voice", None)
    channel = None if state is None else state.channel
    return channel if isinstance(channel, discord.VoiceChannel | discord.StageChannel) else None


def may_manage(member: Any) -> bool:
    """Whether a member holds Manage Channels (and so may move, stop or skip for others)."""
    permissions = getattr(member, "guild_permissions", None)
    return bool(permissions is not None and permissions.manage_channels)


async def require_audio(bot: AhaBot, ctx: commands.Context[commands.Bot]) -> AudioBackend | None:
    """The backend, or tell the user playback is off or the node is down."""
    audio = bot.audio
    locale = locale_of(ctx)
    if audio is None:
        await reply(ctx, embed=embeds.error(t("voice.off", locale)), ephemeral=True)
        return None
    if not audio.up:
        await reply(ctx, embed=embeds.error(t("voice.node_down", locale)), ephemeral=True)
        return None
    return audio


async def precheck(
    bot: AhaBot,
    ctx: commands.Context[commands.Bot],
    guild: discord.Guild,
    audio: AudioBackend,
    channel: VocalChannel,
) -> str | None:
    """May the bot go to ``channel``?

    ``"same"`` when it is already there, ``"ok"`` when it may join or move, ``None`` after replying
    with the reason it may not.
    """
    locale = locale_of(ctx)
    permissions = channel.permissions_for(guild.me) if guild.me else None
    if permissions is not None and not (permissions.connect and permissions.speak):
        message = t("voice.missing_permissions", locale, channel=plain(channel.name))
        await reply(ctx, embed=embeds.error(message), ephemeral=True)
        return None
    current = audio.channel_id(guild.id)
    if current == channel.id:
        return "same"
    if current is None and audio.player_count() >= bot.settings.voice_max_players:
        await reply(ctx, embed=embeds.error(t("voice.busy", locale)), ephemeral=True)
        return None
    if current is not None and audio.is_playing(guild.id) and not may_manage(ctx.author):
        await reply(ctx, embed=embeds.error(t("voice.in_use", locale)), ephemeral=True)
        return None
    return "ok"


def title_link(title: str, video: str) -> str:
    """A video title as a link to the video.

    The title is untrusted: it is cut at 100 characters, markdown and mentions are neutralised, and
    brackets become parentheses so it cannot end our link early and start another one.
    """
    text = clip(plain(title), 100).replace("[", "(").replace("]", ")")
    return f"[{text}]({watch_url(video)})"
