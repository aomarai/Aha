"""Tests for the voice queue's logic, against an in-memory queue store and a fake audio backend."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, cast

import pytest

from aha.playback import MAX_START_ATTEMPTS, TITLE_LIMIT, PlaybackQueue
from aha.queuestore import QueueFullError
from aha.voice import Track, VoiceError
from tests.helpers import FakeQueueStore


class FakeAudio:
    """Records what the queue asks of the audio backend."""

    def __init__(self) -> None:
        self.playing: set[int] = set()
        self.paused: set[int] = set()
        self.plays: list[tuple[int, str]] = []
        self.stops: list[int] = []
        self.left: list[int] = []
        self.pauses: list[tuple[int, bool]] = []
        self.load_errors: dict[str, VoiceError] = {}
        self.play_error: VoiceError | None = None
        self.streams: set[str] = set()
        self.stop_error: VoiceError | None = None
        self.loaded: list[str] = []

    def is_playing(self, guild_id: int) -> bool:
        return guild_id in self.playing

    def is_paused(self, guild_id: int) -> bool:
        return guild_id in self.paused

    async def load(self, video: str) -> Track:
        self.loaded.append(video)
        if video in self.load_errors:
            raise self.load_errors[video]
        return Track(video, f"title {video}", "a", 1000, video in self.streams, f"h-{video}")

    async def play(self, guild_id: int, track: Track) -> None:
        if self.play_error is not None:
            raise self.play_error
        self.plays.append((guild_id, track.video_id))
        self.playing.add(guild_id)

    async def stop_track(self, guild_id: int) -> None:
        self.stops.append(guild_id)
        if self.stop_error is not None:
            raise self.stop_error
        self.playing.discard(guild_id)

    async def leave(self, guild_id: int) -> None:
        self.left.append(guild_id)
        self.playing.discard(guild_id)

    async def set_paused(self, guild_id: int, paused: bool) -> None:
        self.pauses.append((guild_id, paused))
        (self.paused.add if paused else self.paused.discard)(guild_id)


def make(max_queue: int = 50) -> tuple[PlaybackQueue, FakeAudio, FakeQueueStore]:
    audio, store = FakeAudio(), FakeQueueStore()
    return PlaybackQueue(cast("Any", audio), cast("Any", store), max_queue=max_queue), audio, store


def track(video: str = "aaaaaaaaaaa", title: str = "A song", length_ms: int = 213000) -> Track:
    return Track(video, title, "a", length_ms, False, "handle")


def add(queue: PlaybackQueue, video: str, requester: int = 7, guild: int = 1) -> Any:
    return asyncio.run(queue.add(guild, track(video), requester))


def video(n: int) -> str:
    return f"{n:011d}"


# ----------------------------------------------------------------------------- adding


def test_adding_stores_the_track_with_its_requester_and_the_limit() -> None:
    queue, _, store = make(max_queue=7)

    entry = asyncio.run(queue.add(1, track("bbbbbbbbbbb", "My *song*", 5000), 42))

    assert (entry.guild_id, entry.video_id, entry.title, entry.length_ms) == (
        1,
        "bbbbbbbbbbb",
        "My *song*",
        5000,
    )
    assert entry.requester_id == 42
    assert store.calls == [("add", 1, "bbbbbbbbbbb", "My *song*", 5000, 42, 7)]


def test_a_long_title_is_cut_before_it_is_stored() -> None:
    queue, _, _ = make()

    entry = asyncio.run(queue.add(1, track(title="t" * 500), 1))

    assert len(entry.title) == TITLE_LIMIT == 100
    assert entry.title.endswith("…")


def test_a_full_queue_refuses_more() -> None:
    queue, _, _ = make(max_queue=2)
    add(queue, video(1))
    add(queue, video(2))

    with pytest.raises(QueueFullError):
        add(queue, video(3))


def test_entries_are_listed_in_order_up_to_the_queue_limit() -> None:
    queue, _, store = make(max_queue=2)
    add(queue, video(1))
    add(queue, video(2))

    assert [e.video_id for e in asyncio.run(queue.entries(1))] == [video(1), video(2)]
    assert ("entries", 1, 2) in store.calls


# ----------------------------------------------------------------------------- starting


def test_the_head_of_the_queue_is_started_and_remembered_as_current() -> None:
    queue, audio, _ = make()
    first = add(queue, video(1))
    add(queue, video(2))

    started = asyncio.run(queue.ensure_playing(1))

    assert started == first
    assert audio.plays == [(1, video(1))]
    assert queue.current_id(1) == first.id
    assert queue.current_id(2) is None


def test_nothing_is_started_while_something_plays() -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    audio.playing.add(1)

    assert asyncio.run(queue.ensure_playing(1)) is None
    assert audio.plays == [] and audio.loaded == []


def test_an_empty_queue_starts_nothing_and_forgets_the_current_track() -> None:
    queue, audio, _ = make()
    queue._current[1] = 99

    assert asyncio.run(queue.ensure_playing(1)) is None
    assert queue.current_id(1) is None and audio.plays == []


def test_a_track_that_cannot_be_loaded_is_dropped_and_the_next_is_tried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue, audio, store = make()
    add(queue, video(1))
    second = add(queue, video(2))
    audio.load_errors[video(1)] = VoiceError("voice.load_failed")

    with caplog.at_level(logging.WARNING, logger="aha.playback"):
        started = asyncio.run(queue.ensure_playing(1))

    assert started == second
    assert [r.video_id for r in store.rows] == [video(2)]
    assert caplog.messages == [f"Dropping queued video {video(1)}: voice.load_failed"]


def test_a_live_stream_in_the_queue_is_dropped() -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    second = add(queue, video(2))
    audio.streams.add(video(1))

    assert asyncio.run(queue.ensure_playing(1)) == second
    assert audio.plays == [(1, video(2))]


def test_a_play_failure_drops_the_track_too() -> None:
    queue, audio, store = make()
    add(queue, video(1))
    audio.play_error = VoiceError("voice.play_failed")

    assert asyncio.run(queue.ensure_playing(1)) is None
    assert store.rows == []


def test_it_gives_up_after_a_few_failures_in_a_row() -> None:
    queue, audio, store = make()
    for n in range(MAX_START_ATTEMPTS + 2):
        add(queue, video(n))
        audio.load_errors[video(n)] = VoiceError("voice.load_failed")

    assert asyncio.run(queue.ensure_playing(1)) is None
    assert len(audio.loaded) == MAX_START_ATTEMPTS == 3
    assert len(store.rows) == 2  # the rest wait for the next try


def test_not_being_in_a_channel_keeps_the_queue_for_later() -> None:
    queue, audio, store = make()
    add(queue, video(1))
    audio.play_error = VoiceError("voice.not_connected")

    assert asyncio.run(queue.ensure_playing(1)) is None
    assert [r.video_id for r in store.rows] == [video(1)]
    assert queue.current_id(1) is None


# ----------------------------------------------------------------------------- advancing


def test_when_a_track_ends_it_is_finished_and_the_next_one_starts() -> None:
    queue, audio, store = make()
    first = add(queue, video(1))
    second = add(queue, video(2))
    asyncio.run(queue.ensure_playing(1))
    audio.playing.clear()  # the node reports the track as ended

    started = asyncio.run(queue.advance(1))

    assert started == second
    assert [r.id for r in store.rows] == [second.id]
    assert queue.current_id(1) == second.id
    assert ("finish", 1, first.id) in store.calls


def test_the_last_track_ending_leaves_an_empty_queue() -> None:
    queue, audio, store = make()
    add(queue, video(1))
    asyncio.run(queue.ensure_playing(1))
    audio.playing.clear()

    assert asyncio.run(queue.advance(1)) is None
    assert store.rows == [] and queue.current_id(1) is None


def test_an_end_event_for_a_video_that_is_not_playing_is_ignored() -> None:
    queue, audio, store = make()
    add(queue, video(1))
    second = add(queue, video(2))
    add(queue, video(3))
    asyncio.run(queue.ensure_playing(1))
    audio.playing.clear()
    assert asyncio.run(queue.advance(1, video=video(1))) == second  # the real end

    # The same end arrives again, late: the second track is playing now and must not be finished.
    assert asyncio.run(queue.advance(1, video=video(1))) is None
    assert [r.video_id for r in store.rows] == [video(2), video(3)]
    assert queue.current_id(1) == second.id


def test_an_end_event_with_nothing_playing_here_is_ignored() -> None:
    queue, _, store = make()
    add(queue, video(1))

    assert asyncio.run(queue.advance(1, video=video(1))) is None
    assert len(store.rows) == 1


def test_a_stuck_track_is_stopped_then_replaced() -> None:
    queue, audio, store = make()
    add(queue, video(1))
    second = add(queue, video(2))
    asyncio.run(queue.ensure_playing(1))  # still marked as playing: it is stuck, not ended

    started = asyncio.run(queue.advance(1, stop_first=True))

    assert audio.stops == [1]
    assert started == second
    assert audio.plays == [(1, video(1)), (1, video(2))]
    assert [r.id for r in store.rows] == [second.id]


def test_a_stop_that_fails_does_not_stop_the_queue_moving_on() -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    add(queue, video(2))
    asyncio.run(queue.ensure_playing(1))
    audio.stop_error = VoiceError("voice.play_failed")

    assert asyncio.run(queue.advance(1, stop_first=True)) is not None


# ------------------------------------------------------------------ skipping and stopping


def test_skipping_finishes_the_current_track_and_plays_the_next() -> None:
    queue, audio, store = make()
    first = add(queue, video(1))
    second = add(queue, video(2))
    asyncio.run(queue.ensure_playing(1))

    started = asyncio.run(queue.skip(1))

    assert started == second
    assert audio.plays[-1] == (1, video(2))
    assert audio.stops == []  # the new track replaced the old one
    assert [r.id for r in store.rows] == [second.id] and first.id not in [r.id for r in store.rows]


def test_skipping_the_last_track_stops_playback() -> None:
    queue, audio, store = make()
    add(queue, video(1))
    asyncio.run(queue.ensure_playing(1))

    assert asyncio.run(queue.skip(1)) is None

    assert audio.stops == [1]
    assert store.rows == [] and queue.current_id(1) is None


def test_stopping_clears_the_queue_and_leaves() -> None:
    queue, audio, store = make()
    add(queue, video(1))
    add(queue, video(2))
    add(queue, video(3), guild=2)
    asyncio.run(queue.ensure_playing(1))

    dropped = asyncio.run(queue.stop(1))

    assert dropped == 2
    assert audio.left == [1]
    assert [r.guild_id for r in store.rows] == [2]  # the other guild is untouched
    assert queue.current_id(1) is None


# ----------------------------------------------------------------------------- removing, pausing


def test_a_waiting_track_can_be_removed_but_not_the_one_playing() -> None:
    queue, _, store = make()
    first = add(queue, video(1))
    second = add(queue, video(2))
    asyncio.run(queue.ensure_playing(1))

    assert asyncio.run(queue.remove(1, first.id)) is None  # that is /skip
    removed = asyncio.run(queue.remove(1, second.id))

    assert removed == second
    assert [r.id for r in store.rows] == [first.id]
    assert asyncio.run(queue.remove(1, second.id)) is None  # already gone


def test_discarding_takes_back_a_track_that_never_started() -> None:
    queue, _, store = make()
    entry = add(queue, video(1))

    asyncio.run(queue.discard(1, entry.id))

    assert store.rows == []


def test_pausing_toggles_and_reports_the_new_state() -> None:
    queue, audio, _ = make()

    assert asyncio.run(queue.toggle_pause(1)) is True
    assert asyncio.run(queue.toggle_pause(1)) is False

    assert audio.pauses == [(1, True), (1, False)]


# ----------------------------------------------------------------------------- concurrency


def test_a_track_ending_and_a_skip_at_once_are_handled_one_after_the_other() -> None:
    queue, audio, store = make()
    for n in range(1, 5):
        add(queue, video(n))
    asyncio.run(queue.ensure_playing(1))

    async def both() -> None:
        audio.playing.clear()  # the end event of the first track
        await asyncio.gather(queue.advance(1, video=video(1)), queue.skip(1))

    asyncio.run(both())

    assert audio.plays == [(1, video(1)), (1, video(2)), (1, video(3))]  # each started once
    assert [r.video_id for r in store.rows] == [video(3), video(4)]
    assert queue.current_id(1) == store.rows[0].id


def test_two_ensure_playing_calls_at_once_start_one_track() -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    add(queue, video(2))

    async def both() -> list[Any]:
        return list(await asyncio.gather(queue.ensure_playing(1), queue.ensure_playing(1)))

    results = asyncio.run(both())

    assert audio.plays == [(1, video(1))]
    assert results.count(None) == 1


def test_advancing_with_no_track_remembered_starts_the_head_of_the_saved_queue() -> None:
    queue, audio, store = make()
    first = add(queue, video(1))
    add(queue, video(2))  # the queue survived a restart; nothing is remembered as current

    started = asyncio.run(queue.advance(1))

    assert started == first
    assert audio.plays == [(1, video(1))]
    assert [r.video_id for r in store.rows] == [video(1), video(2)]  # nothing was finished


# ------------------------------------------------------------------------ the details


def test_a_plain_advance_does_not_stop_the_node_first() -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    add(queue, video(2))
    asyncio.run(queue.ensure_playing(1))
    audio.playing.clear()

    asyncio.run(queue.advance(1))

    assert audio.stops == []


def test_an_advance_while_the_node_still_plays_does_not_start_a_second_track() -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    add(queue, video(2))
    asyncio.run(queue.ensure_playing(1))  # still playing: the end event raced a new track

    assert asyncio.run(queue.advance(1)) is None
    assert len(audio.plays) == 1


def test_skipping_the_last_track_survives_a_node_that_refuses_to_stop() -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    asyncio.run(queue.ensure_playing(1))
    audio.stop_error = VoiceError("voice.play_failed")

    assert asyncio.run(queue.skip(1)) is None  # must not raise


def test_stopping_with_nothing_remembered_does_not_fail() -> None:
    queue, audio, _ = make()

    assert asyncio.run(queue.stop(1)) == 0
    assert audio.left == [1]


def test_nothing_about_a_track_is_remembered_after_it_is_gone() -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    asyncio.run(queue.ensure_playing(1))
    assert queue._current_video == {1: video(1)}

    asyncio.run(queue.stop(1))
    assert queue._current_video == {} and queue._current == {}

    add(queue, video(2))
    asyncio.run(queue.ensure_playing(1))
    audio.playing.clear()
    asyncio.run(queue.advance(1))  # the second ends; the queue is empty now
    assert queue._current_video == {} and queue._current == {}


def test_a_live_stream_is_dropped_with_its_reason_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    audio.streams.add(video(1))

    with caplog.at_level(logging.WARNING, logger="aha.playback"):
        asyncio.run(queue.ensure_playing(1))

    assert caplog.messages == [f"Dropping queued video {video(1)}: voice.live_unsupported"]


def test_two_guilds_never_wait_for_each_other() -> None:
    queue, audio, _ = make()
    add(queue, video(1), guild=1)
    add(queue, video(2), guild=2)
    gate = asyncio.Event()
    original_load = audio.load

    async def slow_load(video_id: str) -> Track:
        if video_id == video(1):
            await gate.wait()  # guild 1's start hangs here, holding guild 1's lock
        return await original_load(video_id)

    cast("Any", audio).load = slow_load

    async def scenario() -> list[Any]:
        first = asyncio.create_task(queue.ensure_playing(1))
        await asyncio.sleep(0)
        other = await asyncio.wait_for(queue.ensure_playing(2), timeout=1)  # not blocked
        gate.set()
        return [other, await first]

    other, first = asyncio.run(scenario())

    assert other is not None and first is not None
    assert sorted(audio.plays) == [(1, video(1)), (2, video(2))]


@pytest.mark.parametrize("operation", ["advance", "skip", "stop"])
def test_a_slow_operation_in_one_guild_never_blocks_another(operation: str) -> None:
    queue, audio, _ = make()
    for guild in (1, 2):
        add(queue, video(guild), guild=guild)
        add(queue, video(guild + 10), guild=guild)
        asyncio.run(queue.ensure_playing(guild))
        audio.playing.clear()
    gate = asyncio.Event()
    original_load, original_leave = audio.load, audio.leave

    async def slow_load(video_id: str) -> Track:
        if video_id == video(11):  # guild 1's second track: its start hangs
            await gate.wait()
        return await original_load(video_id)

    async def slow_leave(guild_id: int) -> None:
        if guild_id == 1:
            await gate.wait()
        await original_leave(guild_id)

    cast("Any", audio).load = slow_load
    cast("Any", audio).leave = slow_leave
    run_for: Callable[[int], Coroutine[Any, Any, Any]] = {
        "advance": queue.advance,
        "skip": queue.skip,
        "stop": queue.stop,
    }[operation]

    async def scenario() -> None:
        slow: asyncio.Task[Any] = asyncio.create_task(run_for(1))
        await asyncio.sleep(0)
        await asyncio.wait_for(run_for(2), timeout=1)  # guild 2 is not held up by guild 1
        gate.set()
        await slow

    asyncio.run(scenario())


def test_finishing_a_track_forgets_it_even_when_the_next_cannot_start() -> None:
    queue, audio, _ = make()
    add(queue, video(1))
    add(queue, video(2))
    asyncio.run(queue.ensure_playing(1))
    audio.playing.clear()
    audio.play_error = VoiceError("voice.not_connected")  # the next one cannot start: no channel

    assert asyncio.run(queue.advance(1)) is None

    assert queue._current == {} and queue._current_video == {}


def test_an_empty_queue_clears_a_stale_video_as_well_as_a_stale_entry() -> None:
    queue, _, _ = make()
    queue._current[1] = 5
    queue._current_video[1] = video(5)

    assert asyncio.run(queue.ensure_playing(1)) is None

    assert queue._current == {} and queue._current_video == {}
