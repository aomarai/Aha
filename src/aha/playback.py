"""The voice queue's logic (#171): what plays next, and what happens when a track ends.

The queue itself lives in PostgreSQL (:mod:`aha.queuestore`), so it survives a restart: the lowest
id of a guild is the track playing now, and finishing a track deletes its row. This class owns the
decisions on top of it and talks to the audio node through :class:`aha.voice.AudioBackend`.

Everything for one guild runs under that guild's lock, so a track ending, a skip and a ``/play``
arriving together cannot start two tracks or finish one twice. Finishing is idempotent anyway (a
repeated end event deletes nothing). A queued track that cannot be loaded any more (removed,
blocked) is dropped and the next one is tried, a few times, instead of stalling the queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aha.queuestore import QueueEntry, QueueStore
from aha.textutil import clip
from aha.voice import AudioBackend, Track, VoiceError

__all__ = ["MAX_START_ATTEMPTS", "TITLE_LIMIT", "PlaybackQueue"]

log = logging.getLogger(__name__)

#: Longest title stored (a title is untrusted text that comes back out in messages).
TITLE_LIMIT = 100

#: Queued tracks tried in a row before giving up on starting one (each failure drops that track).
MAX_START_ATTEMPTS = 3


class PlaybackQueue:
    """Queue and playback decisions for every guild this process is in a voice channel for."""

    def __init__(self, audio: AudioBackend, store: QueueStore, *, max_queue: int) -> None:
        self._audio = audio
        self._store = store
        self._max_queue = max_queue
        #: The queue entry the node is playing in each guild (in memory: a restart drops it, and
        #: the next ``/play`` or ``/resume`` starts the head of the stored queue again).
        self._current: dict[int, int] = {}
        #: The video of that entry, so a late or repeated end event for another one is ignored.
        self._current_video: dict[int, str] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    def current_id(self, guild_id: int) -> int | None:
        """The id of the queue entry playing now, if this process started one."""
        return self._current.get(guild_id)

    # -- reading ---------------------------------------------------------------------------------

    async def entries(self, guild_id: int) -> list[QueueEntry]:
        """The guild's queue in play order (the first is playing, or next to play)."""
        return await self._store.entries(guild_id, self._max_queue)

    # -- adding and removing ---------------------------------------------------------------------

    async def add(self, guild_id: int, track: Track, requester_id: int) -> QueueEntry:
        """Queue a track. Raises :class:`aha.queuestore.QueueFullError` at the limit."""
        return await self._store.add(
            guild_id,
            video_id=track.video_id,
            title=clip(track.title, TITLE_LIMIT),
            length_ms=track.length_ms,
            requester_id=requester_id,
            limit=self._max_queue,
        )

    async def remove(self, guild_id: int, entry_id: int) -> QueueEntry | None:
        """Delete a queued track (never the one playing: that is :meth:`skip`)."""
        if self._current.get(guild_id) == entry_id:
            return None
        return await self._store.remove(guild_id, entry_id)

    async def discard(self, guild_id: int, entry_id: int) -> None:
        """Take back a track that was queued but could not be started."""
        await self._store.remove(guild_id, entry_id)

    # -- playing ---------------------------------------------------------------------------------

    async def ensure_playing(self, guild_id: int) -> QueueEntry | None:
        """Start the head of the queue unless something is already playing.

        Returns the entry that started, or ``None`` (something plays, or nothing could start).
        """
        async with self._lock(guild_id):
            if self._audio.is_playing(guild_id):
                return None
            return await self._start(guild_id)

    async def advance(
        self, guild_id: int, *, stop_first: bool = False, video: str | None = None
    ) -> QueueEntry | None:
        """The current track is over: delete it and start the next.

        ``video`` is the video the node says ended; an event for any other video than the one this
        process started (a repeat, or one that arrives late) is ignored, so it cannot finish the
        track that is playing now. ``stop_first`` is for a track the node says is stuck (it never
        ended): it is stopped before the next one starts. Returns the entry that started.
        """
        async with self._lock(guild_id):
            if video is not None and self._current_video.get(guild_id) != video:
                return None
            await self._finish_current(guild_id)
            if stop_first:
                with contextlib.suppress(VoiceError):
                    await self._audio.stop_track(guild_id)
                return await self._start(guild_id)
            if self._audio.is_playing(guild_id):
                return None  # e.g. a track the user queued as this one ended
            return await self._start(guild_id)

    async def skip(self, guild_id: int) -> QueueEntry | None:
        """Skip the current track. Returns the entry that started next, or ``None`` if none is."""
        async with self._lock(guild_id):
            await self._finish_current(guild_id)
            started = await self._start(guild_id)
            if started is None:
                with contextlib.suppress(VoiceError):
                    await self._audio.stop_track(guild_id)
            return started

    async def stop(self, guild_id: int) -> int:
        """Clear the guild's queue and leave the channel. Returns how many tracks were dropped."""
        async with self._lock(guild_id):
            self._current.pop(guild_id, None)
            self._current_video.pop(guild_id, None)
            dropped = await self._store.clear(guild_id)
            await self._audio.leave(guild_id)
            return dropped

    async def toggle_pause(self, guild_id: int) -> bool:
        """Pause a playing track or resume a paused one. Returns whether it is paused now."""
        paused = not self._audio.is_paused(guild_id)
        await self._audio.set_paused(guild_id, paused)
        return paused

    # -- internals -------------------------------------------------------------------------------

    async def _finish_current(self, guild_id: int) -> None:
        current = self._current.pop(guild_id, None)
        self._current_video.pop(guild_id, None)
        if current is not None:
            await self._store.finish(guild_id, current)

    async def _start(self, guild_id: int) -> QueueEntry | None:
        """Play the head of the queue, replacing what plays now (caller holds the guild's lock)."""
        for _ in range(MAX_START_ATTEMPTS):
            head = await self._store.head(guild_id)
            if head is None:
                self._current.pop(guild_id, None)
                self._current_video.pop(guild_id, None)
                return None
            try:
                track = await self._audio.load(head.video_id)
                if track.is_stream:
                    raise VoiceError("voice.live_unsupported")
                await self._audio.play(guild_id, track)
            except VoiceError as error:
                if error.key == "voice.not_connected":
                    return None  # not in a channel: keep the queue, it plays after the next join
                log.warning("Dropping queued video %s: %s", head.video_id, error.key)
                await self._store.finish(guild_id, head.id)
                continue
            self._current[guild_id] = head.id
            self._current_video[guild_id] = head.video_id
            return head
        return None
