"""Offline tests for the voice cog (/join, /leave and the idle janitor), against a fake backend."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import discord
import pytest

from aha.cogs.voice import JANITOR_TASK, SWEEP_INTERVAL, Voice
from aha.cogs.voice import setup as voice_setup
from aha.config import Settings
from aha.voice import VoiceError
from tests.helpers import Named, StubContext, as_context


class FakeAudio:
    """Records what the cog asks of the audio backend."""

    def __init__(self) -> None:
        self.up = True
        self.players: dict[int, int | None] = {}
        self.playing: set[int] = set()
        self.joined: list[Any] = []
        self.left: list[int] = []
        self.join_error: VoiceError | None = None

    def player_count(self) -> int:
        return len(self.players)

    def guild_ids(self) -> list[int]:
        return list(self.players)

    def channel_id(self, guild_id: int) -> int | None:
        return self.players.get(guild_id)

    def is_playing(self, guild_id: int) -> bool:
        return guild_id in self.playing

    async def join(self, channel: Any) -> None:
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

    @property
    def voice_states(self) -> dict[int, discord.VoiceState]:
        return {n: cast("Any", None) for n in range(self._members_in_voice)}

    def permissions_for(self, obj: Any, /) -> discord.Permissions:
        return self.perms


class FakeBot:
    def __init__(self, audio: FakeAudio | None, **settings: Any) -> None:
        self.audio = audio
        self.settings = Settings(token="t", **settings)
        self.tasks = FakeTasks()
        self.channels: dict[int, Any] = {}
        self.cogs: list[Any] = []

    def get_channel(self, channel_id: int) -> Any:
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
