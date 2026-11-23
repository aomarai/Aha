"""Offline tests for the voice cog (/join, /leave and the idle janitor), against a fake backend."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, cast

import discord
import pytest

from aha.cogs.voice import JANITOR_TASK, SWEEP_INTERVAL, Voice
from aha.cogs.voice import setup as voice_setup
from aha.config import Settings
from aha.metrics import Metrics
from aha.playback import PlaybackQueue
from aha.voice import AudioEvent, Track, VoiceError
from tests.helpers import FakeQueueStore, Named, StubContext, as_context


class FakeAudio:
    """Records what the cog asks of the audio backend."""

    def __init__(self) -> None:
        self.up = True
        self.players: dict[int, int | None] = {}
        self.playing: set[int] = set()
        self.paused: set[int] = set()
        self.stops: list[int] = []
        self.joined: list[Any] = []
        self.left: list[int] = []
        self.join_error: VoiceError | None = None
        self.loads: list[str] = []
        self.load_result: Track = Track(
            "dQw4w9WgXcQ", "Never Gonna Give You Up", "Rick", 213000, False, "h"
        )
        self.load_error: VoiceError | None = None
        self.plays: list[tuple[int, Track]] = []
        self.play_error: VoiceError | None = None
        self.order: list[str] = []
        self.listeners: list[Any] = []

    def player_count(self) -> int:
        return len(self.players)

    def guild_ids(self) -> list[int]:
        return list(self.players)

    def channel_id(self, guild_id: int) -> int | None:
        return self.players.get(guild_id)

    def is_playing(self, guild_id: int) -> bool:
        return guild_id in self.playing

    def is_paused(self, guild_id: int) -> bool:
        return guild_id in self.paused

    def position_ms(self, guild_id: int) -> int:
        return 62_000

    async def set_paused(self, guild_id: int, paused: bool) -> None:
        (self.paused.add if paused else self.paused.discard)(guild_id)

    async def stop_track(self, guild_id: int) -> None:
        self.stops.append(guild_id)
        self.playing.discard(guild_id)

    async def load(self, video: str) -> Track:
        self.loads.append(video)
        self.order.append("load")
        if self.load_error is not None:
            raise self.load_error
        return dataclasses.replace(self.load_result, video_id=video)

    async def play(self, guild_id: int, track: Track) -> None:
        self.order.append("play")
        if self.play_error is not None:
            raise self.play_error
        self.plays.append((guild_id, track))
        self.playing.add(guild_id)

    def subscribe(self, listener: Any) -> None:
        self.listeners.append(listener)

    async def join(self, channel: Any) -> None:
        self.order.append("join")
        if self.join_error is not None:
            raise self.join_error
        self.joined.append(channel)
        self.players[channel.guild.id] = channel.id

    async def leave(self, guild_id: int) -> None:
        self.left.append(guild_id)
        self.players.pop(guild_id, None)
        self.playing.discard(guild_id)

    async def run(self) -> None:  # pragma: no cover - not used by the cog
        raise NotImplementedError


class FakeTasks:
    def __init__(self) -> None:
        self.spawned: list[dict[str, Any]] = []
        self.names: set[str] = set()

    def __contains__(self, name: str) -> bool:
        return name in self.names

    def spawn(self, factory: Any, **kwargs: Any) -> None:
        self.spawned.append({"factory": factory, **kwargs})
        self.names.add(kwargs["name"])


class FakeVoiceChannel(discord.VoiceChannel):
    """A real ``VoiceChannel`` (so ``isinstance`` holds) with its state faked."""

    def __init__(
        self, channel_id: int = 10, name: str = "General", *, guild_id: int = 11, others: int = 1
    ) -> None:
        self.id = channel_id
        self.name = name
        self.guild = cast("Any", Named(id=guild_id))
        self._members_in_voice = others + 1  # the bot counts itself
        self.perms = discord.Permissions(connect=True, speak=True)
        self.asked_about: list[Any] = []

    @property
    def voice_states(self) -> dict[int, discord.VoiceState]:
        return {n: cast("Any", None) for n in range(self._members_in_voice)}

    def permissions_for(self, obj: Any, /) -> discord.Permissions:
        self.asked_about.append(obj)
        return self.perms


class FakeBot:
    def __init__(self, audio: FakeAudio | None, **settings: Any) -> None:
        self.audio = audio
        self.settings = Settings(token="t", **settings)
        self.tasks = FakeTasks()
        self.channels: dict[int, Any] = {}
        self.looked_up: list[int] = []
        self.cogs: list[Any] = []
        self.metrics = Metrics(lambda: [], lambda: {})
        self.store = FakeQueueStore()
        self.playback = (
            PlaybackQueue(
                cast("Any", audio), cast("Any", self.store), max_queue=self.settings.voice_max_queue
            )
            if audio is not None
            else None
        )

    def get_channel(self, channel_id: int) -> Any:
        self.looked_up.append(channel_id)
        return self.channels.get(channel_id)

    async def add_cog(self, cog: Any) -> None:
        self.cogs.append(cog)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(audio: FakeAudio | None = None, clock: Clock | None = None, **settings: Any) -> Voice:
    bot = FakeBot(audio, **settings)
    return Voice(cast("Any", bot), clock=clock or Clock())


def ctx_in(
    channel: FakeVoiceChannel | None, *, manage_channels: bool = False, with_me: bool = True
) -> StubContext:
    ctx = StubContext()
    ctx.guild = Named(id=11, me=Named(id=1) if with_me else None)
    ctx.author = Named(
        id=33,
        display_name="Sam",
        voice=Named(channel=channel) if channel is not None else None,
        guild_permissions=discord.Permissions(manage_channels=manage_channels),
    )
    return ctx


def run(cog: Voice, command: Any, ctx: StubContext) -> None:
    asyncio.run(cast("Any", command.callback)(cog, as_context(ctx)))


def text(ctx: StubContext) -> str:
    ((_, kwargs),) = ctx.replies
    return cast("str", kwargs["embed"].description)


def ephemeral(ctx: StubContext) -> bool:
    ((_, kwargs),) = ctx.replies
    return bool(kwargs.get("ephemeral", False))


# ----------------------------------------------------------------------------- /join


def test_join_without_lavalink_says_playback_is_off() -> None:
    ctx = ctx_in(FakeVoiceChannel())

    run(make(None), Voice.join, ctx)

    assert text(ctx) == "Voice playback is not available in this deployment."
    assert ephemeral(ctx) is True and ctx.deferred == []


def test_join_when_the_node_is_down_says_so() -> None:
    audio = FakeAudio()
    audio.up = False
    ctx = ctx_in(FakeVoiceChannel())

    run(make(audio), Voice.join, ctx)

    assert text(ctx) == "The audio server isn't reachable right now. Try again in a minute."
    assert audio.joined == []


def test_join_needs_the_caller_to_be_in_a_voice_channel() -> None:
    audio = FakeAudio()
    for caller in (None, FakeVoiceChannel()):
        ctx = ctx_in(caller)
        if caller is None:
            ctx.author = Named(id=33)  # no voice attribute at all
        else:
            cast("Any", ctx.author).voice = Named(channel=Named(id=5))  # not a voice channel
        run(make(audio), Voice.join, ctx)
        assert text(ctx) == "Join a voice channel first, then run this again."
        assert ephemeral(ctx) is True
    assert audio.joined == []


def test_join_checks_connect_and_speak_in_the_callers_channel() -> None:
    audio = FakeAudio()
    channel = FakeVoiceChannel(name="Secret *room*")
    for perms in (
        discord.Permissions(connect=True),
        discord.Permissions(speak=True),
        discord.Permissions.none(),
    ):
        channel.perms = perms
        ctx = ctx_in(channel)
        run(make(audio), Voice.join, ctx)
        assert text(ctx) == "I need Connect and Speak in **Secret \\*room\\*** to join it."
    assert audio.joined == []


def test_join_skips_the_permission_check_when_the_bot_member_is_not_cached() -> None:
    audio = FakeAudio()
    channel = FakeVoiceChannel()
    channel.perms = discord.Permissions.none()
    ctx = ctx_in(channel, with_me=False)

    run(make(audio), Voice.join, ctx)

    assert audio.joined == [channel]  # Discord will refuse if it must


def test_join_connects_defers_and_confirms_publicly() -> None:
    audio = FakeAudio()
    channel = FakeVoiceChannel(name="General")
    ctx = ctx_in(channel)
    cog = make(audio)
    cog._idle_since[11] = 5.0

    run(cog, Voice.join, ctx)

    assert audio.joined == [channel]
    assert ctx.deferred == [False]
    assert text(ctx) == "Joined **General**."
    assert ephemeral(ctx) is False
    assert 11 not in cog._idle_since  # a fresh join restarts the idle clock


def test_join_when_already_in_that_channel_says_so_and_does_nothing() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    ctx = ctx_in(FakeVoiceChannel(channel_id=10, name="General"))

    run(make(audio), Voice.join, ctx)

    assert text(ctx) == "I'm already in **General**."
    assert audio.joined == [] and ctx.deferred == []


def test_join_is_refused_when_this_process_is_at_its_player_limit() -> None:
    audio = FakeAudio()
    audio.players = {101: 1, 102: 2}
    ctx = ctx_in(FakeVoiceChannel())

    run(make(audio, voice_max_players=2), Voice.join, ctx)

    assert text(ctx) == "I'm busy with other voice sessions right now. Try again later."
    assert audio.joined == []


def test_join_is_allowed_one_below_the_player_limit() -> None:
    audio = FakeAudio()
    audio.players = {101: 1}
    ctx = ctx_in(FakeVoiceChannel())

    run(make(audio, voice_max_players=2), Voice.join, ctx)

    assert len(audio.joined) == 1


def test_moving_to_another_channel_does_not_count_against_the_limit() -> None:
    audio = FakeAudio()
    audio.players = {11: 10, 101: 1}  # this guild already holds one of the two places
    ctx = ctx_in(FakeVoiceChannel(channel_id=12))

    run(make(audio, voice_max_players=2), Voice.join, ctx)

    assert len(audio.joined) == 1


def test_the_bot_will_not_leave_music_for_someone_without_manage_channels() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    audio.playing.add(11)
    ctx = ctx_in(FakeVoiceChannel(channel_id=12))

    run(make(audio), Voice.join, ctx)

    assert text(ctx) == (
        "I'm playing in another channel. Ask someone with Manage Channels, or wait until it's done."
    )
    assert audio.joined == []


def test_manage_channels_may_move_the_bot_while_it_plays() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    audio.playing.add(11)
    ctx = ctx_in(FakeVoiceChannel(channel_id=12), manage_channels=True)

    run(make(audio), Voice.join, ctx)

    assert len(audio.joined) == 1


def test_anyone_may_move_an_idle_bot() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    ctx = ctx_in(FakeVoiceChannel(channel_id=12))

    run(make(audio), Voice.join, ctx)

    assert len(audio.joined) == 1


def test_a_failed_join_tells_the_user_why() -> None:
    audio = FakeAudio()
    audio.join_error = VoiceError("voice.join_failed")
    ctx = ctx_in(FakeVoiceChannel())

    run(make(audio), Voice.join, ctx)

    assert text(ctx) == "I couldn't join that channel. Check my permissions and try again."
    assert ephemeral(ctx) is True


def test_join_answers_in_the_users_language() -> None:
    ctx = ctx_in(FakeVoiceChannel(name="Lobby"))
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)

    run(make(FakeAudio()), Voice.join, ctx)

    assert text(ctx) == "**Lobby** beigetreten."


# ----------------------------------------------------------------------------- /leave


def test_leave_without_lavalink_says_playback_is_off() -> None:
    ctx = ctx_in(None)

    run(make(None), Voice.leave, ctx)

    assert text(ctx) == "Voice playback is not available in this deployment."


def test_leave_when_not_in_a_channel_says_so() -> None:
    audio = FakeAudio()
    ctx = ctx_in(FakeVoiceChannel())

    run(make(audio), Voice.leave, ctx)

    assert text(ctx) == "I'm not in a voice channel."
    assert audio.left == []


def test_leave_needs_the_caller_in_the_same_channel() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    for caller in (None, FakeVoiceChannel(channel_id=99)):
        ctx = ctx_in(caller)
        run(make(audio), Voice.leave, ctx)
        assert text(ctx) == "Join my voice channel first, or ask someone with Manage Channels."
    assert audio.left == []


def test_leave_from_the_same_channel_disconnects_and_confirms() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    ctx = ctx_in(FakeVoiceChannel(channel_id=10))
    cog = make(audio)
    cog._idle_since[11] = 5.0

    run(cog, Voice.leave, ctx)

    assert audio.left == [11]
    assert ctx.deferred == [False]
    assert text(ctx) == "Left the voice channel."
    assert 11 not in cog._idle_since


def test_manage_channels_may_send_the_bot_away_from_outside() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    ctx = ctx_in(None, manage_channels=True)

    run(make(audio), Voice.leave, ctx)

    assert audio.left == [11]


# ----------------------------------------------------------------------------- listeners


def test_listeners_count_everyone_but_the_bot() -> None:
    cog = make(FakeAudio())
    cast("Any", cog.bot).channels = {
        10: FakeVoiceChannel(others=3),
        11: FakeVoiceChannel(others=0),
    }

    assert cog.listeners(10) == 3
    assert cog.listeners(11) == 0


def test_a_channel_that_is_gone_counts_as_empty() -> None:
    cog = make(FakeAudio())

    assert cog.listeners(404) == 0


def test_listeners_never_go_negative_when_the_cache_is_behind() -> None:
    cog = make(FakeAudio())
    empty = FakeVoiceChannel()
    empty._members_in_voice = 0
    cast("Any", cog.bot).channels = {10: empty}

    assert cog.listeners(10) == 0


# ----------------------------------------------------------------------------- the janitor


def sweeper(audio: FakeAudio, clock: Clock, *, others: dict[int, int], **settings: Any) -> Voice:
    cog = make(audio, clock, **settings)
    for channel_id, count in others.items():
        cast("Any", cog.bot).channels[channel_id] = FakeVoiceChannel(
            channel_id=channel_id, others=count
        )
    return cog


def test_the_janitor_does_nothing_without_a_backend_or_sessions() -> None:
    assert asyncio.run(make(None).sweep()) == []
    assert asyncio.run(make(FakeAudio()).sweep()) == []


def test_a_silent_session_is_left_after_the_idle_time() -> None:
    audio, clock = FakeAudio(), Clock()
    audio.players[11] = 10
    cog = sweeper(audio, clock, others={10: 2}, voice_idle_minutes=5)

    assert asyncio.run(cog.sweep()) == []  # the clock starts now
    clock.now += 299
    assert asyncio.run(cog.sweep()) == []  # one second short
    clock.now += 1
    assert asyncio.run(cog.sweep()) == [11]  # exactly the idle time
    assert audio.left == [11]
    assert 11 not in cog._idle_since


def test_a_playing_session_with_listeners_is_never_idle() -> None:
    audio, clock = FakeAudio(), Clock()
    audio.players[11] = 10
    audio.playing.add(11)
    cog = sweeper(audio, clock, others={10: 1}, voice_idle_minutes=1)

    asyncio.run(cog.sweep())
    clock.now += 3600

    assert asyncio.run(cog.sweep()) == []
    assert cog._idle_since == {}


def test_an_empty_channel_is_left_even_while_playing() -> None:
    audio, clock = FakeAudio(), Clock()
    audio.players[11] = 10
    audio.playing.add(11)
    cog = sweeper(audio, clock, others={10: 0}, voice_idle_minutes=1)

    asyncio.run(cog.sweep())
    clock.now += 60

    assert asyncio.run(cog.sweep()) == [11]


def test_a_session_whose_channel_vanished_counts_as_alone() -> None:
    audio, clock = FakeAudio(), Clock()
    audio.players[11] = None
    cog = sweeper(audio, clock, others={}, voice_idle_minutes=1)

    asyncio.run(cog.sweep())
    clock.now += 60

    assert asyncio.run(cog.sweep()) == [11]


def test_the_idle_clock_restarts_when_someone_comes_back() -> None:
    audio, clock = FakeAudio(), Clock()
    audio.players[11] = 10
    cog = sweeper(audio, clock, others={10: 0}, voice_idle_minutes=5)

    asyncio.run(cog.sweep())  # alone, clock starts at 1000
    clock.now += 200
    cast("Any", cog.bot).channels[10] = FakeVoiceChannel(channel_id=10, others=2)
    audio.playing.add(11)
    asyncio.run(cog.sweep())  # listeners and playing: the clock is cleared
    clock.now += 200
    cast("Any", cog.bot).channels[10] = FakeVoiceChannel(channel_id=10, others=0)
    audio.playing.clear()
    asyncio.run(cog.sweep())  # idle again from now, not from 1000

    clock.now += 299
    assert asyncio.run(cog.sweep()) == []
    clock.now += 1
    assert asyncio.run(cog.sweep()) == [11]


def test_sessions_that_ended_are_forgotten_and_guilds_are_independent() -> None:
    audio, clock = FakeAudio(), Clock()
    audio.players = {11: 10, 12: 20}
    cog = sweeper(audio, clock, others={10: 0, 20: 0}, voice_idle_minutes=1)
    asyncio.run(cog.sweep())
    assert set(cog._idle_since) == {11, 12}

    del audio.players[12]  # that guild's session ended by itself
    clock.now += 60
    left = asyncio.run(cog.sweep())

    assert left == [11] and audio.left == [11]
    assert cog._idle_since == {}


def test_leaving_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    audio, clock = FakeAudio(), Clock()
    audio.players[11] = 10
    cog = sweeper(audio, clock, others={10: 0}, voice_idle_minutes=2)
    asyncio.run(cog.sweep())
    clock.now += 120

    with caplog.at_level(logging.INFO, logger="aha.cogs.voice"):
        asyncio.run(cog.sweep())

    assert caplog.messages == ["Left the voice channel of guild 11 after 2 idle minutes"]


def test_the_janitor_loop_sweeps_then_sleeps_the_sweep_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cog = make(FakeAudio())
    events: list[str] = []

    async def sweep() -> list[int]:
        events.append("sweep")
        return []

    async def sleep(seconds: float) -> None:
        events.append(f"sleep {seconds:g}")
        if events.count("sweep") == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(cog, "sweep", sweep)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cog._janitor())

    assert events == ["sweep", "sleep 30", "sweep", "sleep 30"]
    assert SWEEP_INTERVAL == 30.0


# ----------------------------------------------------------------------------- lifecycle


def test_loading_the_cog_starts_one_restartable_janitor() -> None:
    cog = make(FakeAudio())

    asyncio.run(cog.cog_load())
    asyncio.run(cog.cog_load())  # a second load must not start a second one

    (spawn,) = cast("Any", cog.bot).tasks.spawned
    assert spawn["name"] == JANITOR_TASK == "voice-janitor"
    assert spawn["restart"] is True
    assert spawn["factory"] == cog._janitor
    assert "critical" not in spawn  # losing voice must not make the bot unready


def test_setup_adds_the_cog() -> None:
    bot = FakeBot(None)

    asyncio.run(voice_setup(cast("Any", bot)))

    (cog,) = bot.cogs
    assert isinstance(cog, Voice)


def test_the_command_metadata() -> None:
    for command, description in (
        (Voice.join, "Join your voice channel."),
        (Voice.leave, "Leave the voice channel."),
    ):
        assert command.extras["category"] == "Fun"
        assert command.description == description
        cooldown = command.cooldown
        assert cooldown is not None and (cooldown.rate, cooldown.per) == (1, 5)
    assert Voice.join.qualified_name == "join" and Voice.leave.qualified_name == "leave"


# ----------------------------------------------------------------------------- the details


def test_without_lavalink_join_is_private_and_in_the_users_language() -> None:
    ctx = ctx_in(FakeVoiceChannel())
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)

    run(make(None), Voice.join, ctx)

    assert text(ctx) == "Sprachwiedergabe ist in dieser Installation nicht verfügbar."
    assert ephemeral(ctx) is True


def test_a_down_node_is_reported_privately_and_in_the_users_language() -> None:
    audio = FakeAudio()
    audio.up = False
    ctx = ctx_in(FakeVoiceChannel())
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)

    run(make(audio), Voice.join, ctx)

    assert text(ctx) == (
        "Der Audioserver ist gerade nicht erreichbar. Versuche es in einer Minute noch einmal."
    )
    assert ephemeral(ctx) is True


def test_an_author_without_permissions_is_treated_as_not_a_manager() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    audio.playing.add(11)
    ctx = ctx_in(FakeVoiceChannel(channel_id=12))
    ctx.author = Named(
        id=33, voice=Named(channel=FakeVoiceChannel(channel_id=12))
    )  # no permissions

    run(make(audio), Voice.join, ctx)

    assert "playing in another channel" in text(ctx)
    assert audio.joined == []


def test_one_busy_guild_does_not_stop_the_others_being_swept() -> None:
    audio, clock = FakeAudio(), Clock()
    audio.players = {11: 10, 12: 20}
    audio.playing.add(11)
    cog = sweeper(audio, clock, others={10: 2, 20: 0}, voice_idle_minutes=1)
    asyncio.run(cog.sweep())
    clock.now += 60

    assert asyncio.run(cog.sweep()) == [12]  # 11 is playing for listeners; 12 is alone and idle


# ----------------------------------------------------------------------------- /play

LINK = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def run_play(cog: Voice, ctx: StubContext, link: str = LINK) -> None:
    asyncio.run(cast("Any", Voice.play.callback)(cog, as_context(ctx), link))


def outcome_count(cog: Voice, outcome: str) -> float:
    value = cast("Any", cog.bot).metrics.registry.get_sample_value(
        "aha_voice_tracks_total", {"outcome": outcome}
    )
    return float(value or 0.0)


def counts(cog: Voice) -> dict[str, float]:
    return {o: outcome_count(cog, o) for o in ("ok", "refused", "failed")}


def test_play_loads_joins_then_plays_and_announces_the_track() -> None:
    audio = FakeAudio()
    channel = FakeVoiceChannel(channel_id=10)
    ctx = ctx_in(channel)
    cast("Any", ctx.author).display_name = "Some *one*"
    cog = make(audio)
    cog._idle_since[11] = 5.0

    run_play(cog, ctx)

    assert audio.loads == ["dQw4w9WgXcQ", "dQw4w9WgXcQ"]  # to check it, then to start it
    # Loaded to check it, then joined, then the queue loads it again to start it (a fresh stream).
    assert audio.order == ["load", "join", "load", "play"]
    assert audio.joined == [channel]
    assert [(g, t.video_id) for g, t in audio.plays] == [(11, "dQw4w9WgXcQ")]
    assert ctx.deferred == [False]
    assert text(ctx) == (
        "▶️ Now playing [Never Gonna Give You Up](https://www.youtube.com/watch?v=dQw4w9WgXcQ) "
        "(3:33), requested by Some \\*one\\*."
    )
    assert ephemeral(ctx) is False
    assert counts(cog) == {"ok": 1.0, "refused": 0.0, "failed": 0.0}
    assert cog._announce == {11: 22}  # where a playback problem will be reported
    assert 11 not in cog._idle_since


def test_play_does_not_rejoin_a_channel_it_is_already_in() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    ctx = ctx_in(FakeVoiceChannel(channel_id=10))

    run_play(make(audio), ctx)

    assert audio.order == ["load", "load", "play"]  # no join: it is already in the channel


def test_a_bad_link_is_refused_before_anything_else_happens() -> None:
    audio = FakeAudio()
    ctx = ctx_in(FakeVoiceChannel())
    cog = make(audio)

    run_play(cog, ctx, "https://evil.example/watch?v=dQw4w9WgXcQ")

    assert text(ctx).startswith("That isn't a YouTube video link.")
    assert ephemeral(ctx) is True and ctx.deferred == []
    assert audio.order == []
    assert counts(cog) == {"ok": 0.0, "refused": 1.0, "failed": 0.0}


def test_a_playlist_link_gets_its_own_message() -> None:
    ctx = ctx_in(FakeVoiceChannel())

    run_play(make(FakeAudio()), ctx, "https://www.youtube.com/playlist?list=PLabcdefghij")

    assert text(ctx) == "Playlists aren't supported yet. Paste the link of a single video."


def test_play_without_lavalink_or_with_the_node_down_says_so_before_looking_at_the_link() -> None:
    ctx = ctx_in(FakeVoiceChannel())
    run_play(make(None), ctx, "not a link")
    assert text(ctx) == "Voice playback is not available in this deployment."

    audio = FakeAudio()
    audio.up = False
    ctx = ctx_in(FakeVoiceChannel())
    run_play(make(audio), ctx, "not a link")
    assert text(ctx).startswith("The audio server isn't reachable")


def test_play_needs_the_caller_in_a_voice_channel() -> None:
    audio = FakeAudio()
    ctx = ctx_in(None)

    run_play(make(audio), ctx)

    assert text(ctx) == "Join a voice channel first, then run this again."
    assert audio.order == []


def test_play_uses_the_same_permission_busy_and_in_use_checks_as_join() -> None:
    channel = FakeVoiceChannel()
    channel.perms = discord.Permissions.none()
    ctx = ctx_in(channel)
    run_play(make(FakeAudio()), ctx)
    assert text(ctx).startswith("I need Connect and Speak in **General**")

    audio = FakeAudio()
    audio.players = {101: 1}
    ctx = ctx_in(FakeVoiceChannel())
    run_play(make(audio, voice_max_players=1), ctx)
    assert text(ctx).startswith("I'm busy with other voice sessions")

    audio = FakeAudio()
    audio.players[11] = 10
    audio.playing.add(11)
    ctx = ctx_in(FakeVoiceChannel(channel_id=12))
    run_play(make(audio), ctx)
    assert text(ctx).startswith("I'm playing in another channel")
    assert audio.loads == []


def test_play_while_something_plays_queues_the_track_and_says_where() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    audio.playing.add(11)
    ctx = ctx_in(FakeVoiceChannel(channel_id=10))
    cog = make(audio)
    store = cast("Any", cog.bot).store
    asyncio.run(
        store.add(11, video_id="aaaaaaaaaaa", title="x", length_ms=1, requester_id=1, limit=50)
    )

    run_play(cog, ctx)

    assert text(ctx) == (
        "🎶 Queued [Never Gonna Give You Up](https://www.youtube.com/watch?v=dQw4w9WgXcQ) "
        "(3:33) at position 2, requested by Sam."
    )
    assert audio.plays == [] and audio.joined == []  # something else is already playing
    assert ephemeral(ctx) is False
    assert counts(cog)["ok"] == 1.0


def test_a_live_stream_is_refused_and_nothing_is_joined() -> None:
    audio = FakeAudio()
    audio.load_result = Track("dQw4w9WgXcQ", "Live", "x", 1, True, "h")
    ctx = ctx_in(FakeVoiceChannel())
    cog = make(audio)

    run_play(cog, ctx)

    assert text(ctx) == "Live streams aren't supported. Paste a regular video."
    assert ephemeral(ctx) is True
    assert audio.order == ["load"]
    assert cast("Any", cog.bot).store.rows == []  # nothing was queued
    assert counts(cog)["refused"] == 1.0


def test_a_track_over_the_limit_is_refused_and_one_at_the_limit_is_not() -> None:
    audio = FakeAudio()
    audio.load_result = Track("dQw4w9WgXcQ", "Long", "x", 30 * 60_000 + 1, False, "h")
    ctx = ctx_in(FakeVoiceChannel())
    cog = make(audio)
    run_play(cog, ctx)
    assert text(ctx) == "That video is longer than 30 minutes, which is the limit."
    assert audio.order == ["load"]

    audio = FakeAudio()
    audio.load_result = Track("dQw4w9WgXcQ", "Just right", "x", 30 * 60_000, False, "h")
    ctx = ctx_in(FakeVoiceChannel())
    run_play(make(audio), ctx)
    assert audio.order == ["load", "join", "load", "play"]


def test_the_length_limit_comes_from_the_setting() -> None:
    audio = FakeAudio()
    audio.load_result = Track("dQw4w9WgXcQ", "Five minutes", "x", 5 * 60_000 + 1, False, "h")
    ctx = ctx_in(FakeVoiceChannel())

    run_play(make(audio, voice_max_track_minutes=5), ctx)

    assert text(ctx) == "That video is longer than 5 minutes, which is the limit."


@pytest.mark.parametrize(
    ("stage", "message"),
    [
        ("load", "I couldn't load that video."),
        ("join", "I couldn't load that video."),  # the error's own message is shown
        ("play", "I couldn't start playback. Try again in a moment."),
    ],
)
def test_a_failure_at_any_stage_is_reported_privately_counted_and_leaves_no_track_queued(
    stage: str, message: str
) -> None:
    audio = FakeAudio()
    setattr(audio, f"{stage}_error", VoiceError("voice.load_failed"))
    ctx = ctx_in(FakeVoiceChannel())
    cog = make(audio)

    run_play(cog, ctx)

    assert text(ctx).startswith(message)
    assert ephemeral(ctx) is True
    assert counts(cog) == {"ok": 0.0, "refused": 0.0, "failed": 1.0}
    assert cog._announce == {}
    assert cast("Any", cog.bot).store.rows == []  # the track was taken back, or dropped


def test_play_without_a_database_says_why_before_anything_else() -> None:
    audio = FakeAudio()
    cog = make(audio)
    cast("Any", cog.bot).playback = None
    ctx = ctx_in(FakeVoiceChannel())

    run_play(cog, ctx)

    assert (
        text(ctx) == "Voice playback needs a database for the queue, and this deployment has none."
    )
    assert ephemeral(ctx) is True and audio.order == []


def test_a_full_queue_is_refused_with_its_limit() -> None:
    audio = FakeAudio()
    audio.players[11] = 10
    audio.playing.add(11)
    cog = make(audio, voice_max_queue=1)
    store = cast("Any", cog.bot).store
    asyncio.run(
        store.add(11, video_id="aaaaaaaaaaa", title="x", length_ms=1, requester_id=1, limit=50)
    )
    ctx = ctx_in(FakeVoiceChannel(channel_id=10))

    run_play(cog, ctx)

    assert (
        text(ctx) == "The queue is full (1 tracks). Wait for something to play, or remove a track."
    )
    assert ephemeral(ctx) is True
    assert counts(cog)["refused"] == 1.0
    assert len(store.rows) == 1


def test_a_hostile_title_cannot_turn_into_another_link() -> None:
    audio = FakeAudio()
    audio.load_result = Track(
        "dQw4w9WgXcQ", "x](https://evil.example)[y @everyone " + "z" * 200, "a", 1000, False, "h"
    )
    ctx = ctx_in(FakeVoiceChannel())

    run_play(make(audio), ctx)

    shown = text(ctx)
    assert "](https://evil.example)" not in shown
    assert shown.count("](") == 1  # only our own link
    assert shown.split("](")[1].startswith("https://www.youtube.com/watch?v=dQw4w9WgXcQ)")
    assert "@\u200beveryone" in shown  # mentions are neutralised
    assert "z" * 100 not in shown  # the title is cut


def test_play_answers_in_german() -> None:
    ctx = ctx_in(FakeVoiceChannel())
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)

    run_play(make(FakeAudio()), ctx)

    assert text(ctx).startswith("▶️ Es läuft [Never Gonna Give You Up]")
    assert text(ctx).endswith("(3:33), gewünscht von Sam.")


def test_the_length_is_shown_as_minutes_and_seconds_or_with_hours() -> None:
    from aha.cogs.playback import format_length

    assert [format_length(ms) for ms in (0, 999, 1000, 59_000, 213_000, 3_599_000, 3_723_000)] == [
        "0:00",
        "0:00",
        "0:01",
        "0:59",
        "3:33",
        "59:59",
        "1:02:03",
    ]


# ----------------------------------------------------------------------------- playback problems


class Reporter(discord.abc.Messageable):
    def __init__(
        self, error: Exception | None = None, locale: discord.Locale | None = None
    ) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.error = error
        self.guild = Named(preferred_locale=locale) if locale is not None else None

    async def _get_channel(self) -> Any:  # pragma: no cover - never reached
        raise NotImplementedError

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        if self.error is not None:
            raise self.error
        self.sent.append((cast("str", content), kwargs))


def event(guild_id: int = 11) -> AudioEvent:
    return AudioEvent(guild_id, "failed", "dQw4w9WgXcQ", "suspicious: x")


def test_a_playback_problem_is_reported_once_to_the_channel_of_the_last_play() -> None:
    cog = make(FakeAudio())
    channel = Reporter(locale=discord.Locale.american_english)
    cast("Any", cog.bot).channels[22] = channel
    cog._announce[11] = 22

    asyncio.run(cog._on_audio_event(event()))

    ((message, kwargs),) = channel.sent
    assert (
        message
        == "That track stopped because of a playback problem. Try again, or try another video."
    )
    assert (
        kwargs["allowed_mentions"].everyone is False and kwargs["allowed_mentions"].users is False
    )
    assert counts(cog)["failed"] == 1.0


def test_the_report_follows_the_servers_language() -> None:
    cog = make(FakeAudio())
    channel = Reporter(locale=discord.Locale.german)
    cast("Any", cog.bot).channels[22] = channel
    cog._announce[11] = 22

    asyncio.run(cog._on_audio_event(event()))

    assert channel.sent[0][0].startswith("Dieser Titel wurde wegen eines Wiedergabeproblems")


def test_a_channel_without_a_guild_gets_the_default_language() -> None:
    cog = make(FakeAudio())
    channel = Reporter()
    cast("Any", cog.bot).channels[22] = channel
    cog._announce[11] = 22

    asyncio.run(cog._on_audio_event(event()))

    assert channel.sent[0][0].startswith("That track stopped")


def test_a_problem_with_nowhere_to_report_is_only_counted() -> None:
    cog = make(FakeAudio())

    asyncio.run(cog._on_audio_event(event(99)))  # no /play from this guild: must not raise

    assert counts(cog)["failed"] == 1.0


def test_a_report_that_cannot_be_sent_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tests.helpers import http_error

    cog = make(FakeAudio())
    cast("Any", cog.bot).channels[22] = Reporter(error=http_error())
    cog._announce[11] = 22

    with caplog.at_level(logging.INFO, logger="aha.cogs.voice"):
        asyncio.run(cog._on_audio_event(event()))

    assert caplog.messages == ["Could not report a playback problem (HTTPException)"]


def test_the_cog_listens_for_playback_problems_when_there_is_a_backend() -> None:
    audio = FakeAudio()
    cog = make(audio)

    asyncio.run(cog.cog_load())
    asyncio.run(cog.cog_load())

    assert audio.listeners == [cog._on_audio_event]  # once, even if loaded twice
    asyncio.run(make(None).cog_load())  # and nothing to listen to without a backend: no error


def test_play_metadata() -> None:
    assert Voice.play.qualified_name == "play"
    assert Voice.play.extras["category"] == "Fun"
    assert Voice.play.description == "Play a YouTube link in your voice channel."
    cooldown = Voice.play.cooldown
    assert cooldown is not None and (cooldown.rate, cooldown.per) == (1, 5)


# ----------------------------------------------------------------------------- the details of /play


def german(ctx: StubContext) -> StubContext:
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)
    return ctx


def test_the_checks_before_joining_reply_privately_and_in_the_users_language() -> None:
    channel = FakeVoiceChannel(name="Lobby")
    channel.perms = discord.Permissions.none()
    ctx = german(ctx_in(channel))
    run(make(FakeAudio()), Voice.join, ctx)
    assert text(ctx) == "Ich brauche „Verbinden“ und „Sprechen“ in **Lobby**, um beizutreten."
    assert ephemeral(ctx) is True

    audio = FakeAudio()
    audio.players = {101: 1}
    ctx = german(ctx_in(FakeVoiceChannel()))
    run(make(audio, voice_max_players=1), Voice.join, ctx)
    assert text(ctx) == (
        "Ich bin gerade mit anderen Sprachsitzungen beschäftigt. Versuche es später noch einmal."
    )
    assert ephemeral(ctx) is True

    audio = FakeAudio()
    audio.players[11] = 10
    audio.playing.add(11)
    ctx = german(ctx_in(FakeVoiceChannel(channel_id=12)))
    run(make(audio), Voice.join, ctx)
    assert text(ctx).startswith("Ich spiele gerade in einem anderen Kanal.")
    assert ephemeral(ctx) is True


def test_play_refuses_in_the_users_language() -> None:
    audio = FakeAudio()
    audio.load_result = Track("dQw4w9WgXcQ", "Live", "x", 1, True, "h")
    ctx = german(ctx_in(FakeVoiceChannel()))
    run_play(make(audio), ctx)
    assert text(ctx) == "Livestreams werden nicht unterstützt. Füge ein normales Video ein."

    audio = FakeAudio()
    audio.load_result = Track("dQw4w9WgXcQ", "Long", "x", 31 * 60_000, False, "h")
    ctx = german(ctx_in(FakeVoiceChannel()))
    run_play(make(audio), ctx)
    assert text(ctx) == "Das Video ist länger als 30 Minuten, das ist die Grenze."


def test_a_long_title_is_cut_at_a_hundred_characters_and_brackets_become_parentheses() -> None:
    audio = FakeAudio()
    audio.load_result = Track("dQw4w9WgXcQ", "a[b]c" + "x" * 200, "a", 1000, False, "h")
    ctx = ctx_in(FakeVoiceChannel())

    run_play(make(audio), ctx)

    link = text(ctx).split("[", 1)[1].split("](", 1)[0]
    assert link.startswith("a(b)c")
    assert len(link) == 100 and link.endswith("…")


def test_the_link_keeps_the_title_and_points_at_the_canonical_url() -> None:
    track = Track("dQw4w9WgXcQ", "Plain", "a", 1000, False, "h")

    assert Voice._link(track) == "[Plain](https://www.youtube.com/watch?v=dQw4w9WgXcQ)"


def test_a_problem_in_a_guild_with_no_recent_play_is_not_sent_to_an_unrelated_channel() -> None:
    cog = make(FakeAudio())
    other = Reporter(locale=discord.Locale.american_english)
    cast("Any", cog.bot).channels[1] = other  # id 1 must not be mistaken for "no channel"
    cast("Any", cog.bot).channels[0] = other

    asyncio.run(cog._on_audio_event(event(99)))

    assert other.sent == []


def test_a_channel_with_no_guild_attribute_at_all_gets_the_default_language() -> None:
    class Bare(discord.abc.Messageable):
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def _get_channel(self) -> Any:  # pragma: no cover - never reached
            raise NotImplementedError

        async def send(self, content: str | None = None, **kwargs: Any) -> Any:
            self.sent.append(cast("str", content))

    cog = make(FakeAudio())
    bare = Bare()
    cast("Any", cog.bot).channels[22] = bare
    cog._announce[11] = 22

    asyncio.run(cog._on_audio_event(event()))

    assert bare.sent == [
        "That track stopped because of a playback problem. Try again, or try another video."
    ]


def test_permissions_are_checked_for_the_bots_own_member() -> None:
    channel = FakeVoiceChannel()
    ctx = ctx_in(channel)

    run(make(FakeAudio()), Voice.join, ctx)

    assert channel.asked_about == [cast("Any", ctx.guild).me]


def test_a_problem_in_a_guild_with_no_recent_play_looks_up_no_channel() -> None:
    cog = make(FakeAudio())

    asyncio.run(cog._on_audio_event(event(99)))

    assert cast("Any", cog.bot).looked_up == []


# ----------------------------------------------------------------------------- the queue moves on


def started_cog(videos: int = 2) -> tuple[Voice, FakeAudio]:
    audio = FakeAudio()
    audio.players[11] = 10
    cog = make(audio)
    store = cast("Any", cog.bot).store
    for n in range(videos):
        asyncio.run(
            store.add(
                11, video_id=f"{n:011d}", title=f"t{n}", length_ms=1000, requester_id=7, limit=50
            )
        )
    asyncio.run(cast("Any", cog.bot).playback.ensure_playing(11))
    return cog, audio


def test_when_a_track_ends_the_next_one_starts_and_nothing_is_reported() -> None:
    cog, audio = started_cog()
    audio.playing.clear()  # the node says the first one ended
    channel = Reporter()
    cast("Any", cog.bot).channels[22] = channel
    cog._announce[11] = 22

    asyncio.run(cog._on_audio_event(AudioEvent(11, "ended", f"{0:011d}", "finished")))

    assert [t.video_id for _, t in audio.plays] == [f"{0:011d}", f"{1:011d}"]
    assert channel.sent == []
    assert counts(cog)["failed"] == 0.0


def test_an_end_event_for_another_video_does_not_move_the_queue() -> None:
    cog, audio = started_cog()
    audio.playing.clear()

    asyncio.run(cog._on_audio_event(AudioEvent(11, "ended", "zzzzzzzzzzz", "finished")))

    assert len(audio.plays) == 1


def test_an_end_event_without_a_queue_is_ignored() -> None:
    cog = make(FakeAudio())
    cast("Any", cog.bot).playback = None

    asyncio.run(cog._on_audio_event(AudioEvent(11, "ended", "dQw4w9WgXcQ", "finished")))  # no error


def test_a_failed_track_is_reported_but_the_queue_waits_for_the_end_event() -> None:
    cog, audio = started_cog()
    channel = Reporter()
    cast("Any", cog.bot).channels[22] = channel
    cog._announce[11] = 22

    asyncio.run(cog._on_audio_event(AudioEvent(11, "failed", f"{0:011d}", "suspicious: x")))

    assert len(channel.sent) == 1 and counts(cog)["failed"] == 1.0
    assert len(audio.plays) == 1  # the node follows a failure with an end event, which advances


def test_a_stuck_track_is_reported_and_replaced_by_the_next() -> None:
    cog, audio = started_cog()
    channel = Reporter()
    cast("Any", cog.bot).channels[22] = channel
    cog._announce[11] = 22

    asyncio.run(cog._on_audio_event(AudioEvent(11, "stuck", f"{0:011d}", "no audio")))

    assert len(channel.sent) == 1 and counts(cog)["failed"] == 1.0
    assert [t.video_id for _, t in audio.plays] == [f"{0:011d}", f"{1:011d}"]


def test_a_stuck_event_for_another_video_does_not_skip_what_is_playing() -> None:
    cog, audio = started_cog()

    asyncio.run(cog._on_audio_event(AudioEvent(11, "stuck", "zzzzzzzzzzz", "no audio")))

    assert len(audio.plays) == 1  # reported and counted, but the current track stays
    assert counts(cog)["failed"] == 1.0
