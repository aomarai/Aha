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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import discord
import wavelink

from aha.commands import BotLike
from aha.youtube import watch_url

__all__ = [
    "CONNECT_TIMEOUT",
    "NODE_ID",
    "AudioBackend",
    "AudioEvent",
    "Track",
    "VoiceError",
    "WavelinkBackend",
]

log = logging.getLogger(__name__)

#: Seconds to wait for Discord to accept a join (the voice handshake) before giving up.
CONNECT_TIMEOUT = 15.0

#: The identifier of the one Lavalink node.
NODE_ID = "aha"

#: Why a track ended when the queue should move on: it finished, or it could not be loaded.
_ADVANCING_REASONS = frozenset({"finished", "loadFailed"})


class VoiceError(Exception):
    """A voice action failed for a reason the user can be told; ``key`` is its message key."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True, slots=True)
class Track:
    """A loaded YouTube video, ready to play."""

    video_id: str
    title: str
    author: str
    length_ms: int
    is_stream: bool
    #: The backend's own object for it (a Wavelink ``Playable``); opaque to the cogs.
    handle: Any = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class AudioEvent:
    """Something that went wrong with playback in a guild (the node's words, shortened)."""

    guild_id: int
    #: ``failed`` (the node could not play the track), ``stuck`` (it stopped producing audio) or
    #: ``ended`` (the track is over, by finishing or by failing to load; not by being stopped).
    kind: str
    video_id: str
    #: The node's severity and message, for logs only: never shown to users.
    detail: str


AudioListener = Callable[[AudioEvent], Awaitable[None]]


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

    def is_paused(self, guild_id: int) -> bool:
        """Whether the guild's player is paused."""

    def position_ms(self, guild_id: int) -> int:
        """How far into the current track the guild's player is."""

    async def join(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        """Join ``channel``, or move there. Raises :class:`VoiceError`."""

    async def leave(self, guild_id: int) -> None:
        """Leave the guild's voice channel; a no-op when not in one."""

    async def load(self, video: str) -> Track:
        """Ask the node for a video by id. Raises :class:`VoiceError`."""

    async def play(self, guild_id: int, track: Track) -> None:
        """Start ``track`` in the guild's channel. Raises :class:`VoiceError`."""

    async def set_paused(self, guild_id: int, paused: bool) -> None:
        """Pause or resume the guild's player. Raises :class:`VoiceError`."""

    async def stop_track(self, guild_id: int) -> None:
        """End the current track without starting another. Raises :class:`VoiceError`."""

    def subscribe(self, listener: AudioListener) -> None:
        """Call ``listener`` for every :class:`AudioEvent`."""

    async def run(self) -> None:
        """Keep the node connection alive until cancelled (a supervised task)."""


class WavelinkBackend:
    """:class:`AudioBackend` over a Lavalink node, through Wavelink."""

    def __init__(
        self,
        bot: BotLike,
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
        self._listeners: list[AudioListener] = []
        bot.add_listener(self._on_track_exception, "on_wavelink_track_exception")
        bot.add_listener(self._on_track_stuck, "on_wavelink_track_stuck")
        bot.add_listener(self._on_track_end, "on_wavelink_track_end")

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

    def is_paused(self, guild_id: int) -> bool:
        """Whether the guild's player is paused."""
        player = self._player(guild_id)
        return player is not None and bool(player.paused)

    def position_ms(self, guild_id: int) -> int:
        """The playing position, in milliseconds (0 without a player)."""
        player = self._player(guild_id)
        return 0 if player is None else int(player.position)

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

    # -- tracks ----------------------------------------------------------------------------------

    async def load(self, video: str) -> Track:
        """Ask the node for one video, by the canonical link built from its id."""
        try:
            found = await self._pool.fetch_tracks(watch_url(video))
        except wavelink.LavalinkLoadException as exc:
            log.warning("Loading video %s failed: %s (%s)", video, exc.error[:100], exc.severity)
            raise VoiceError("voice.load_failed") from exc
        except (wavelink.WavelinkException, TimeoutError) as exc:
            log.warning("Loading video %s failed (%s)", video, type(exc).__name__)
            raise VoiceError("voice.load_failed") from exc
        if not isinstance(found, list) or not found:
            raise VoiceError("voice.load_failed")
        playable = found[0]
        return Track(
            video_id=video,
            title=str(playable.title),
            author=str(playable.author),
            length_ms=int(playable.length),
            is_stream=bool(playable.is_stream),
            handle=playable,
        )

    async def play(self, guild_id: int, track: Track) -> None:
        """Play ``track`` on the guild's player."""
        player = self._player(guild_id)
        if player is None:
            raise VoiceError("voice.not_connected")
        try:
            await player.play(track.handle)
        except wavelink.WavelinkException as exc:
            log.warning("Playing video %s failed (%s)", track.video_id, type(exc).__name__)
            raise VoiceError("voice.play_failed") from exc

    async def set_paused(self, guild_id: int, paused: bool) -> None:
        """Pause or resume the guild's player."""
        await self._command(guild_id, lambda player: player.pause(paused))

    async def stop_track(self, guild_id: int) -> None:
        """End the current track; the node reports it as stopped, not as ``ended``."""
        await self._command(guild_id, lambda player: player.stop())

    async def _command(self, guild_id: int, action: Callable[[Any], Awaitable[Any]]) -> None:
        player = self._player(guild_id)
        if player is None:
            raise VoiceError("voice.not_connected")
        try:
            await action(player)
        except wavelink.WavelinkException as exc:
            log.warning("A player command failed (%s)", type(exc).__name__)
            raise VoiceError("voice.play_failed") from exc

    # -- events ----------------------------------------------------------------------------------

    def subscribe(self, listener: AudioListener) -> None:
        """Register a listener for playback problems."""
        self._listeners.append(listener)

    async def _emit(self, payload: Any, kind: str, detail: str) -> AudioEvent | None:
        player = payload.player
        guild = None if player is None else player.guild
        if guild is None:
            return None
        event = AudioEvent(int(guild.id), kind, str(payload.track.identifier), detail)
        for listener in self._listeners:
            await listener(event)
        return event

    async def _problem(self, payload: Any, kind: str, detail: str) -> None:
        """Log a playback problem (for the runbook), then tell the listeners."""
        player = payload.player
        guild = None if player is None else player.guild
        if guild is not None:
            log.warning(
                "Playback %s in guild %s for video %s: %s",
                kind,
                guild.id,
                payload.track.identifier,
                detail,
            )
        await self._emit(payload, kind, detail)

    async def _on_track_exception(self, payload: Any) -> None:
        problem = payload.exception
        detail = f"{problem.get('severity', '?')}: {str(problem.get('message', ''))[:100]}"
        await self._problem(payload, "failed", detail)

    async def _on_track_stuck(self, payload: Any) -> None:
        await self._problem(payload, "stuck", f"no audio for {payload.threshold} ms")

    async def _on_track_end(self, payload: Any) -> None:
        """Only a track that ended by itself (or failed to load) advances the queue: the other
        reasons (stopped, replaced, cleanup) are our own doing."""
        if payload.reason in _ADVANCING_REASONS:
            await self._emit(payload, "ended", str(payload.reason))
