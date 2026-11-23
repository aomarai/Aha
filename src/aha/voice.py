"""Voice playback plumbing (ADR 0002): what the cogs may ask of the audio side, and its adapter.

Audio never runs in this process. A Lavalink node fetches, transcodes and streams it, including
Discord's end-to-end voice encryption; this process only tells the node what to do. Cogs talk to the
small :class:`AudioBackend` protocol, never to Wavelink, so the client library can be replaced
without touching a command (ADR 0002 section 3). A voice session is pinned to the pod that owns the
guild's shard (ADR 0001 section 2), so nothing here is shared between pods and scheduled jobs must
never touch it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any, Protocol

import discord
import wavelink

__all__ = [
    "CONNECT_TIMEOUT",
    "NODE_ID",
    "AudioBackend",
    "VoiceError",
    "WavelinkBackend",
]

log = logging.getLogger(__name__)

#: Seconds to wait for Discord to accept a join (the voice handshake) before giving up.
CONNECT_TIMEOUT = 15.0

#: The identifier of the one Lavalink node.
NODE_ID = "aha"


class VoiceError(Exception):
    """A voice action failed for a reason the user can be told; ``key`` is its message key."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


class AudioBackend(Protocol):
    """What the voice cogs need from the audio side."""

    @property
    def up(self) -> bool:
        """Whether the audio node is connected right now."""

    def player_count(self) -> int:
        """How many guilds this process is in a voice channel for."""

    def guild_ids(self) -> list[int]:
        """The guilds this process is in a voice channel for."""

    def channel_id(self, guild_id: int) -> int | None:
        """The voice channel id the bot is in for a guild, or ``None``."""

    def is_playing(self, guild_id: int) -> bool:
        """Whether something is playing (or paused, which still holds the channel)."""

    async def join(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        """Join ``channel``, or move there. Raises :class:`VoiceError`."""

    async def leave(self, guild_id: int) -> None:
        """Leave the guild's voice channel; a no-op when not in one."""

    async def run(self) -> None:
        """Keep the node connection alive until cancelled (a supervised task)."""


class WavelinkBackend:
    """:class:`AudioBackend` over a Lavalink node, through Wavelink."""

    def __init__(
        self,
        bot: discord.Client,
        url: str,
        password: str,
        *,
        pool: Any = wavelink.Pool,
        node_factory: Callable[..., Any] = wavelink.Node,
    ) -> None:
        self._bot = bot
        self._url = url
        self._password = password
        self._pool = pool
        self._node_factory = node_factory
        #: Reused across connection attempts, so a failed attempt leaves no session behind.
        self._node: Any = None

    # -- the node --------------------------------------------------------------------------------

    async def run(self) -> None:
        """Connect to the node, then wait. Raises when it cannot, so the supervisor retries.

        ``Pool.connect`` logs a failure instead of raising it, so success is checked afterwards.
        Once connected, Wavelink reconnects by itself and resumes the session.
        """
        await self._bot.wait_until_ready()
        if self._node is None:
            self._node = self._node_factory(
                identifier=NODE_ID,
                uri=self._url,
                password=self._password,
                retries=None,
                # Idle players are ended by the voice cog, which also knows who is listening.
                inactive_player_timeout=None,
            )
        await self._pool.connect(nodes=[self._node], client=self._bot)
        if not self._pool.nodes:
            raise ConnectionError("the Lavalink node is not reachable")
        log.info("Connected to the Lavalink node")
        await asyncio.Event().wait()  # until cancelled

    @property
    def up(self) -> bool:
        """Whether a node is connected right now."""
        return any(
            node.status is wavelink.NodeStatus.CONNECTED for node in self._pool.nodes.values()
        )

    def _players(self) -> list[Any]:
        return [player for node in self._pool.nodes.values() for player in node.players.values()]

    # -- players ---------------------------------------------------------------------------------

    def player_count(self) -> int:
        """How many guilds have a player."""
        return len(self._players())

    def guild_ids(self) -> list[int]:
        """The guilds that have a player."""
        return [player.guild.id for player in self._players() if player.guild is not None]

    def _player(self, guild_id: int) -> Any:
        guild = self._bot.get_guild(guild_id)
        voice_client = None if guild is None else guild.voice_client
        return voice_client if isinstance(voice_client, wavelink.Player) else None

    def channel_id(self, guild_id: int) -> int | None:
        """The channel the guild's player is in."""
        player = self._player(guild_id)
        return None if player is None or player.channel is None else int(player.channel.id)

    def is_playing(self, guild_id: int) -> bool:
        """Whether the guild's player is playing or paused."""
        player = self._player(guild_id)
        return player is not None and bool(player.playing or player.paused)

    async def join(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        """Join ``channel`` (deafened: the bot never listens), or move the existing player."""
        if not self.up:
            raise VoiceError("voice.node_down")
        existing = self._player(channel.guild.id)
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                if existing is None:
                    await channel.connect(cls=wavelink.Player, self_deaf=True)
                elif existing.channel is None or existing.channel.id != channel.id:
                    await existing.move_to(channel, self_deaf=True)
        except (TimeoutError, discord.ClientException, wavelink.WavelinkException) as exc:
            log.warning("Joining voice failed (%s)", type(exc).__name__)
            raise VoiceError("voice.join_failed") from exc

    async def leave(self, guild_id: int) -> None:
        """Disconnect the guild's player, if any."""
        player = self._player(guild_id)
        if player is not None:
            await player.disconnect()
