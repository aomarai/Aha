"""Tests for the Lavalink adapter behind voice playback, against a fake node, pool and Discord."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import discord
import pytest
import wavelink

from aha import voice as voice_module
from aha.voice import CONNECT_TIMEOUT, NODE_ID, Track, VoiceError, WavelinkBackend
from tests.helpers import Named


class FakePlayer(wavelink.Player):
    """A real ``wavelink.Player`` (so ``isinstance`` holds) with the state faked."""

    def __init__(self, guild_id: int, channel_id: int | None, *, playing: bool = False) -> None:
        self._fake_guild: Any = Named(id=guild_id)
        self.channel = cast("Any", None if channel_id is None else Named(id=channel_id))
        self._playing = playing
        self._paused = False
        self.moved_to: list[tuple[Any, dict[str, Any]]] = []
        self.disconnected = 0
        self.played: list[Any] = []
        self.play_error: Exception | None = None
        self.pauses: list[bool] = []
        self.stops = 0
        self.command_error: Exception | None = None
        self._position = 0

    @property
    def guild(self) -> Any:
        return self._fake_guild

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def paused(self) -> bool:
        return self._paused

    async def move_to(self, channel: Any, **kwargs: Any) -> None:
        self.moved_to.append((channel, kwargs))

    async def disconnect(self, **kwargs: Any) -> None:
        self.disconnected += 1

    @property
    def position(self) -> int:
        return self._position

    async def pause(self, value: bool, /) -> None:
        if self.command_error is not None:
            raise self.command_error
        self.pauses.append(value)

    async def stop(self, *, force: bool = True) -> Any:
        if self.command_error is not None:
            raise self.command_error
        self.stops += 1

    async def play(self, track: Any, **kwargs: Any) -> Any:
        if self.play_error is not None:
            raise self.play_error
        self.played.append(track)


class FakeNode:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.status = wavelink.NodeStatus.CONNECTED
        self.players: dict[int, FakePlayer] = {}


class FakePool:
    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.fetched: list[str] = []
        self.fetch_result: Any = []
        self.fetch_error: Exception | None = None
        self.nodes: dict[str, FakeNode] = {}
        self.connects: list[tuple[list[FakeNode], Any]] = []

    async def fetch_tracks(self, query: str) -> Any:
        self.fetched.append(query)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.fetch_result

    async def connect(self, *, nodes: list[FakeNode], client: Any) -> dict[str, FakeNode]:
        self.connects.append((nodes, client))
        if self.reachable:
            for node in nodes:
                self.nodes[node.kwargs["identifier"]] = node
        return self.nodes


class FakeGuild:
    def __init__(self, guild_id: int, voice_client: Any = None) -> None:
        self.id = guild_id
        self.voice_client = voice_client


class FakeBot:
    def __init__(self) -> None:
        self.guilds: dict[int, FakeGuild] = {}
        self.ready_waits = 0
        self.listeners: dict[str, Any] = {}

    def add_listener(self, func: Any, name: str) -> None:
        self.listeners[name] = func

    async def wait_until_ready(self) -> None:
        self.ready_waits += 1

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self.guilds.get(guild_id)


class FakeChannel:
    def __init__(self, guild_id: int = 1, channel_id: int = 10, error: Exception | None = None):
        self.guild = Named(id=guild_id)
        self.id = channel_id
        self.error = error
        self.delay = 0.0
        self.connects: list[dict[str, Any]] = []

    async def connect(self, **kwargs: Any) -> None:
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        self.connects.append(kwargs)


def make(pool: FakePool | None = None, bot: FakeBot | None = None) -> tuple[WavelinkBackend, Any]:
    the_pool = pool or FakePool()
    backend = WavelinkBackend(
        cast("Any", bot or FakeBot()),
        "http://aha-lavalink:2333",
        "pw",
        pool=the_pool,
        node_factory=FakeNode,
    )
    return backend, the_pool


def with_player(
    backend: WavelinkBackend, pool: FakePool, bot: FakeBot, player: FakePlayer
) -> FakePlayer:
    """Register ``player`` as the guild's voice client and on the node."""
    node = pool.nodes.setdefault(NODE_ID, FakeNode(identifier=NODE_ID))
    node.players[player.guild.id] = player
    bot.guilds[player.guild.id] = FakeGuild(player.guild.id, player)
    return player


# ----------------------------------------------------------------------------- the node


def run_until_connected(backend: WavelinkBackend) -> asyncio.Task[None]:
    async def go() -> asyncio.Task[None]:
        task = asyncio.create_task(backend.run())
        for _ in range(5):
            await asyncio.sleep(0)
        return task

    return asyncio.run(go())


def test_run_connects_one_node_with_the_configured_address_and_waits() -> None:
    bot, pool = FakeBot(), FakePool()
    backend, _ = make(pool, bot)

    async def scenario() -> bool:
        task = asyncio.create_task(backend.run())
        for _ in range(5):
            await asyncio.sleep(0)
        waiting = not task.done()
        task.cancel()
        return waiting

    assert asyncio.run(scenario()) is True  # connected, and still running (waiting)
    assert bot.ready_waits == 1  # it needs the bot's user id first
    ((nodes, client),) = pool.connects
    (node,) = nodes
    assert client is not None
    assert node.kwargs == {
        "identifier": NODE_ID,
        "uri": "http://aha-lavalink:2333",
        "password": "pw",
        "retries": None,
        "inactive_player_timeout": None,
    }
    assert NODE_ID == "aha"


def test_run_raises_when_the_node_cannot_be_reached_so_the_supervisor_retries() -> None:
    backend, _ = make(FakePool(reachable=False))

    with pytest.raises(ConnectionError) as error:
        asyncio.run(backend.run())

    assert str(error.value) == "the Lavalink node is not reachable"


def test_a_retry_reuses_the_same_node_object() -> None:
    pool = FakePool(reachable=False)
    created: list[FakeNode] = []

    def factory(**kwargs: Any) -> FakeNode:
        node = FakeNode(**kwargs)
        created.append(node)
        return node

    backend = WavelinkBackend(
        cast("Any", FakeBot()), "http://x:1", "pw", pool=pool, node_factory=factory
    )

    for _ in range(2):
        with pytest.raises(ConnectionError):
            asyncio.run(backend.run())

    assert len(created) == 1
    assert pool.connects[0][0][0] is pool.connects[1][0][0]


def test_run_logs_the_connection(caplog: pytest.LogCaptureFixture) -> None:
    backend, _ = make()

    async def scenario() -> None:
        task = asyncio.create_task(backend.run())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()

    with caplog.at_level(logging.INFO, logger="aha.voice"):
        asyncio.run(scenario())

    assert "Connected to the Lavalink node" in caplog.messages


def test_up_means_a_node_is_connected_right_now() -> None:
    backend, pool = make()
    assert backend.up is False  # no node yet

    node = FakeNode(identifier=NODE_ID)
    pool.nodes[NODE_ID] = node
    assert backend.up is True
    node.status = wavelink.NodeStatus.CONNECTING
    assert backend.up is False
    node.status = wavelink.NodeStatus.DISCONNECTED
    assert backend.up is False


# ----------------------------------------------------------------------------- players


def test_it_counts_and_lists_the_guilds_it_is_in_a_voice_channel_for() -> None:
    bot, pool = FakeBot(), FakePool()
    backend, _ = make(pool, bot)
    assert (backend.player_count(), backend.guild_ids()) == (0, [])

    with_player(backend, pool, bot, FakePlayer(1, 10))
    with_player(backend, pool, bot, FakePlayer(2, 20))

    assert backend.player_count() == 2
    assert sorted(backend.guild_ids()) == [1, 2]


def test_the_channel_and_playing_state_come_from_the_guilds_player() -> None:
    bot, pool = FakeBot(), FakePool()
    backend, _ = make(pool, bot)
    with_player(backend, pool, bot, FakePlayer(1, 10, playing=True))
    with_player(backend, pool, bot, FakePlayer(2, 20))
    quiet = with_player(backend, pool, bot, FakePlayer(3, None))
    paused = with_player(backend, pool, bot, FakePlayer(4, 40))
    paused._paused = True

    assert backend.channel_id(1) == 10 and backend.channel_id(2) == 20
    assert backend.channel_id(3) is None and quiet.channel is None
    assert backend.channel_id(99) is None  # not a guild we know
    assert backend.is_playing(1) is True
    assert backend.is_playing(2) is False
    assert backend.is_playing(4) is True  # paused still holds the channel
    assert backend.is_playing(99) is False


def test_a_voice_client_that_is_not_ours_is_ignored() -> None:
    bot = FakeBot()
    bot.guilds[1] = FakeGuild(1, voice_client=object())
    backend, _ = make(bot=bot)

    assert backend.channel_id(1) is None
    assert backend.is_playing(1) is False


def test_a_player_without_a_guild_is_not_listed() -> None:
    bot, pool = FakeBot(), FakePool()
    backend, _ = make(pool, bot)
    orphan = with_player(backend, pool, bot, FakePlayer(1, 10))
    orphan._fake_guild = None

    assert backend.guild_ids() == []
    assert backend.player_count() == 1


# ----------------------------------------------------------------------------- join and leave


def connected(bot: FakeBot | None = None) -> tuple[WavelinkBackend, FakePool, FakeBot]:
    the_bot, pool = bot or FakeBot(), FakePool()
    pool.nodes[NODE_ID] = FakeNode(identifier=NODE_ID)
    backend, _ = make(pool, the_bot)
    return backend, pool, the_bot


def test_joining_connects_a_wavelink_player_deafened() -> None:
    backend, _, _ = connected()
    channel = FakeChannel()

    asyncio.run(backend.join(cast("Any", channel)))

    assert channel.connects == [{"cls": wavelink.Player, "self_deaf": True}]


def test_joining_without_a_node_is_refused_with_a_message_key() -> None:
    backend, _ = make()
    channel = FakeChannel()

    with pytest.raises(VoiceError) as error:
        asyncio.run(backend.join(cast("Any", channel)))

    assert error.value.key == "voice.node_down"
    assert channel.connects == []


def test_joining_the_channel_it_is_already_in_does_nothing() -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))
    channel = FakeChannel(guild_id=1, channel_id=10)

    asyncio.run(backend.join(cast("Any", channel)))

    assert channel.connects == [] and player.moved_to == []


def test_joining_another_channel_moves_the_existing_player() -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))
    channel = FakeChannel(guild_id=1, channel_id=11)

    asyncio.run(backend.join(cast("Any", channel)))

    assert player.moved_to == [(channel, {"self_deaf": True})]
    assert channel.connects == []


def test_a_player_without_a_channel_is_moved_in() -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, None))
    channel = FakeChannel(guild_id=1, channel_id=11)

    asyncio.run(backend.join(cast("Any", channel)))

    assert player.moved_to == [(channel, {"self_deaf": True})]


@pytest.mark.parametrize(
    "error",
    [
        discord.ClientException("already connected"),
        wavelink.WavelinkException("boom"),
        TimeoutError(),
    ],
)
def test_a_failed_join_becomes_a_user_facing_error_and_is_logged(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    backend, _, _ = connected()
    channel = FakeChannel(error=error)

    with caplog.at_level(logging.WARNING, logger="aha.voice"), pytest.raises(VoiceError) as raised:
        asyncio.run(backend.join(cast("Any", channel)))

    assert raised.value.key == "voice.join_failed"
    assert caplog.messages == [f"Joining voice failed ({type(error).__name__})"]


def test_a_join_that_never_finishes_is_given_up_on(monkeypatch: pytest.MonkeyPatch) -> None:
    backend, _, _ = connected()
    channel = FakeChannel()
    channel.delay = 1.0
    monkeypatch.setattr(voice_module, "CONNECT_TIMEOUT", 0.01)

    with pytest.raises(VoiceError) as raised:
        asyncio.run(backend.join(cast("Any", channel)))

    assert raised.value.key == "voice.join_failed"
    assert CONNECT_TIMEOUT == 15.0


def test_leaving_disconnects_the_player_and_is_a_no_op_without_one() -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))

    asyncio.run(backend.leave(1))
    asyncio.run(backend.leave(2))  # nothing there: must not raise

    assert player.disconnected == 1


def test_a_voice_error_carries_its_message_key() -> None:
    error = VoiceError("voice.busy")

    assert error.key == "voice.busy"
    assert str(error) == "voice.busy"
    assert error.args == ("voice.busy",)


# ----------------------------------------------------------------------------- tracks


class FakePlayable:
    def __init__(self, **kwargs: Any) -> None:
        self.title = kwargs.get("title", "Never Gonna Give You Up")
        self.author = kwargs.get("author", "Rick Astley")
        self.length = kwargs.get("length", 213000)
        self.is_stream = kwargs.get("is_stream", False)
        self.identifier = kwargs.get("identifier", "dQw4w9WgXcQ")


def test_loading_asks_the_node_for_the_canonical_link_built_from_the_id() -> None:
    backend, pool = make()
    playable = FakePlayable()
    pool.fetch_result = [playable]

    track = asyncio.run(backend.load("dQw4w9WgXcQ"))

    assert pool.fetched == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
    assert (track.video_id, track.title, track.author) == (
        "dQw4w9WgXcQ",
        "Never Gonna Give You Up",
        "Rick Astley",
    )
    assert (track.length_ms, track.is_stream) == (213000, False)
    assert track.handle is playable


def test_a_stream_is_marked_as_one() -> None:
    backend, pool = make()
    pool.fetch_result = [FakePlayable(is_stream=True, length=9223372036854775807)]

    track = asyncio.run(backend.load("dQw4w9WgXcQ"))

    assert track.is_stream is True


def test_the_first_result_is_used() -> None:
    backend, pool = make()
    pool.fetch_result = [FakePlayable(title="first"), FakePlayable(title="second")]

    assert asyncio.run(backend.load("dQw4w9WgXcQ")).title == "first"


@pytest.mark.parametrize("found", [[], "a playlist object", None])
def test_nothing_usable_found_is_a_load_failure(found: Any) -> None:
    backend, pool = make()
    pool.fetch_result = found

    with pytest.raises(VoiceError) as error:
        asyncio.run(backend.load("dQw4w9WgXcQ"))

    assert error.value.key == "voice.load_failed"


def test_a_node_refusal_is_a_load_failure_and_is_logged_with_its_severity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend, pool = make()
    pool.fetch_error = wavelink.LavalinkLoadException(
        data={
            "message": "Sign in to confirm you're not a bot",
            "severity": "suspicious",
            "cause": "x",
        }
    )

    with caplog.at_level(logging.WARNING, logger="aha.voice"), pytest.raises(VoiceError) as error:
        asyncio.run(backend.load("dQw4w9WgXcQ"))

    assert error.value.key == "voice.load_failed"
    assert caplog.messages == [
        "Loading video dQw4w9WgXcQ failed: Sign in to confirm you're not a bot (suspicious)"
    ]


@pytest.mark.parametrize("error", [wavelink.WavelinkException("x"), TimeoutError()])
def test_other_load_errors_are_load_failures_logged_by_type_only(
    error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    backend, pool = make()
    pool.fetch_error = error

    with caplog.at_level(logging.WARNING, logger="aha.voice"), pytest.raises(VoiceError) as raised:
        asyncio.run(backend.load("dQw4w9WgXcQ"))

    assert raised.value.key == "voice.load_failed"
    assert caplog.messages == [f"Loading video dQw4w9WgXcQ failed ({type(error).__name__})"]


def a_track() -> Any:
    from aha.voice import Track

    return Track("dQw4w9WgXcQ", "t", "a", 1000, False, "the-playable")


def test_playing_hands_the_nodes_object_to_the_guilds_player() -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))

    asyncio.run(backend.play(1, a_track()))

    assert player.played == ["the-playable"]


def test_playing_without_a_player_says_the_bot_is_not_connected() -> None:
    backend, _, _ = connected()

    with pytest.raises(VoiceError) as error:
        asyncio.run(backend.play(1, a_track()))

    assert error.value.key == "voice.not_connected"


def test_a_failed_start_is_reported_and_logged_by_type(caplog: pytest.LogCaptureFixture) -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))
    player.play_error = wavelink.WavelinkException("boom")

    with caplog.at_level(logging.WARNING, logger="aha.voice"), pytest.raises(VoiceError) as error:
        asyncio.run(backend.play(1, a_track()))

    assert error.value.key == "voice.play_failed"
    assert caplog.messages == ["Playing video dQw4w9WgXcQ failed (WavelinkException)"]


# ----------------------------------------------------------------------------- playback problems


def event_payload(player: Any, **extra: Any) -> Any:
    return Named(player=player, track=Named(identifier="dQw4w9WgXcQ"), **extra)


def test_the_backend_listens_for_the_nodes_track_problems() -> None:
    bot = FakeBot()
    make(bot=bot)

    assert sorted(bot.listeners) == [
        "on_wavelink_track_end",
        "on_wavelink_track_exception",
        "on_wavelink_track_stuck",
    ]


def test_a_track_exception_is_passed_on_with_its_severity_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot, pool = FakeBot(), FakePool()
    backend, _ = make(pool, bot)
    seen: list[Any] = []

    async def listener(event: Any) -> None:
        seen.append(event)

    backend.subscribe(listener)
    player = FakePlayer(1, 10)
    payload = event_payload(
        player, exception={"severity": "suspicious", "message": "x" * 300, "cause": "c"}
    )

    with caplog.at_level(logging.WARNING, logger="aha.voice"):
        asyncio.run(bot.listeners["on_wavelink_track_exception"](payload))

    (event,) = seen
    assert (event.guild_id, event.kind, event.video_id) == (1, "failed", "dQw4w9WgXcQ")
    assert event.detail == "suspicious: " + "x" * 100  # the node's message is cut short
    assert caplog.messages == [
        f"Playback failed in guild 1 for video dQw4w9WgXcQ: suspicious: {'x' * 100}"
    ]


def test_a_stuck_track_is_passed_on_with_the_threshold() -> None:
    bot = FakeBot()
    backend, _ = make(bot=bot)
    seen: list[Any] = []

    async def listener(event: Any) -> None:
        seen.append(event)

    backend.subscribe(listener)
    payload = event_payload(FakePlayer(2, 20), threshold=10000)

    asyncio.run(bot.listeners["on_wavelink_track_stuck"](payload))

    (event,) = seen
    assert (event.guild_id, event.kind, event.detail) == (2, "stuck", "no audio for 10000 ms")


def test_every_subscriber_hears_it_and_an_event_without_a_guild_is_ignored() -> None:
    bot = FakeBot()
    backend, _ = make(bot=bot)
    calls: list[str] = []

    async def first(event: Any) -> None:
        calls.append("first")

    async def second(event: Any) -> None:
        calls.append("second")

    backend.subscribe(first)
    backend.subscribe(second)
    lonely = FakePlayer(3, 30)
    lonely._fake_guild = None

    asyncio.run(bot.listeners["on_wavelink_track_stuck"](event_payload(lonely, threshold=1)))
    asyncio.run(bot.listeners["on_wavelink_track_stuck"](event_payload(None, threshold=1)))
    assert calls == []
    asyncio.run(
        bot.listeners["on_wavelink_track_stuck"](event_payload(FakePlayer(4, 40), threshold=1))
    )

    assert calls == ["first", "second"]


def test_the_nodes_message_is_cut_at_a_hundred_characters_in_the_load_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend, pool = make()
    pool.fetch_error = wavelink.LavalinkLoadException(
        data={"message": "m" * 150, "severity": "common", "cause": "x"}
    )

    with caplog.at_level(logging.WARNING, logger="aha.voice"), pytest.raises(VoiceError):
        asyncio.run(backend.load("dQw4w9WgXcQ"))

    assert caplog.messages == [f"Loading video dQw4w9WgXcQ failed: {'m' * 100} (common)"]


def test_an_exception_without_severity_or_message_still_gives_a_readable_detail() -> None:
    bot = FakeBot()
    backend, _ = make(bot=bot)
    seen: list[Any] = []

    async def listener(event: Any) -> None:
        seen.append(event)

    backend.subscribe(listener)

    asyncio.run(
        bot.listeners["on_wavelink_track_exception"](event_payload(FakePlayer(1, 10), exception={}))
    )

    assert seen[0].detail == "?: "


# ----------------------------------------------------------------------------- controls and ends


def test_paused_state_and_position_come_from_the_guilds_player() -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))
    player._paused = True
    player._position = 62_000

    assert backend.is_paused(1) is True
    assert backend.position_ms(1) == 62_000
    assert backend.is_paused(99) is False  # no player
    assert backend.position_ms(99) == 0


def test_pausing_and_resuming_go_to_the_player() -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))

    assert backend.pause_changes(1) == 0
    asyncio.run(backend.set_paused(1, True))
    assert backend.pause_changes(1) == 1
    asyncio.run(backend.set_paused(1, False))

    assert player.pauses == [True, False]
    assert backend.pause_changes(1) == 2
    assert backend.pause_changes(2) == 0  # per guild


def test_a_refused_pause_does_not_count_as_a_change() -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))
    player.command_error = wavelink.WavelinkException("boom")

    with pytest.raises(VoiceError):
        asyncio.run(backend.set_paused(1, True))

    assert backend.pause_changes(1) == 0


def test_leaving_forgets_the_pause_changes() -> None:
    backend, pool, bot = connected()
    with_player(backend, pool, bot, FakePlayer(1, 10))
    asyncio.run(backend.set_paused(1, True))

    asyncio.run(backend.leave(1))

    assert backend.pause_changes(1) == 0


class SlowLeavingPlayer(FakePlayer):
    """A player whose disconnect waits until the test lets it finish."""

    def __init__(self, guild_id: int, channel_id: int | None) -> None:
        super().__init__(guild_id, channel_id)
        self.release = asyncio.Event()
        self.fail_with: Exception | None = None

    async def disconnect(self, **kwargs: Any) -> None:
        await self.release.wait()
        self.disconnected += 1
        if self.fail_with is not None:
            raise self.fail_with


@pytest.mark.parametrize("action", ["pause", "stop", "play"])
def test_commands_are_refused_while_the_player_disconnects(action: str) -> None:
    backend, pool, bot = connected()
    player = SlowLeavingPlayer(1, 10)
    with_player(backend, pool, bot, player)
    track = Track("dQw4w9WgXcQ", "t", "a", 1000, False, "h")

    async def scenario() -> str:
        leaving = asyncio.create_task(backend.leave(1))
        await asyncio.sleep(0)  # the leave is now waiting on the disconnect
        if action == "pause":
            command = backend.set_paused(1, False)
        elif action == "stop":
            command = backend.stop_track(1)
        else:
            command = backend.play(1, track)
        try:
            await command
        except VoiceError as error:
            key = error.key
        else:
            key = "accepted"
        player.release.set()
        await leaving
        return key

    assert asyncio.run(scenario()) == "voice.not_connected"
    assert player.pauses == [] and player.stops == 0 and player.played == []
    assert backend.pause_changes(1) == 0


def test_a_failed_disconnect_does_not_leave_the_guild_blocked() -> None:
    backend, pool, bot = connected()
    player = SlowLeavingPlayer(1, 10)
    with_player(backend, pool, bot, player)
    player.release.set()
    player.fail_with = RuntimeError("gone")

    with pytest.raises(RuntimeError):
        asyncio.run(backend.leave(1))
    asyncio.run(backend.set_paused(1, True))  # the player is still there: commands work again

    assert player.pauses == [True]


def test_stopping_a_track_goes_to_the_player() -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))

    asyncio.run(backend.stop_track(1))

    assert player.stops == 1


@pytest.mark.parametrize("action", ["pause", "stop"])
def test_a_command_without_a_player_says_the_bot_is_not_connected(action: str) -> None:
    backend, _, _ = connected()
    run_action = backend.set_paused(1, True) if action == "pause" else backend.stop_track(1)

    with pytest.raises(VoiceError) as error:
        asyncio.run(run_action)

    assert error.value.key == "voice.not_connected"


@pytest.mark.parametrize("action", ["pause", "stop"])
def test_a_command_the_node_refuses_is_reported_and_logged_by_type(
    action: str, caplog: pytest.LogCaptureFixture
) -> None:
    backend, pool, bot = connected()
    player = with_player(backend, pool, bot, FakePlayer(1, 10))
    player.command_error = wavelink.WavelinkException("boom")
    run_action = backend.set_paused(1, True) if action == "pause" else backend.stop_track(1)

    with caplog.at_level(logging.WARNING, logger="aha.voice"), pytest.raises(VoiceError) as error:
        asyncio.run(run_action)

    assert error.value.key == "voice.play_failed"
    assert caplog.messages == ["A player command failed (WavelinkException)"]


@pytest.mark.parametrize("reason", ["finished", "loadFailed"])
def test_a_track_that_ended_by_itself_or_failed_to_load_advances_the_queue(
    reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    bot = FakeBot()
    backend, _ = make(bot=bot)
    seen: list[Any] = []

    async def listener(event: Any) -> None:
        seen.append(event)

    backend.subscribe(listener)

    with caplog.at_level(logging.WARNING, logger="aha.voice"):
        asyncio.run(
            bot.listeners["on_wavelink_track_end"](event_payload(FakePlayer(1, 10), reason=reason))
        )

    (event,) = seen
    assert (event.guild_id, event.kind, event.video_id, event.detail) == (
        1,
        "ended",
        "dQw4w9WgXcQ",
        reason,
    )
    assert caplog.messages == []  # an ordinary end is not a warning


@pytest.mark.parametrize("reason", ["stopped", "replaced", "cleanup"])
def test_a_track_we_ended_ourselves_does_not_advance_the_queue(reason: str) -> None:
    bot = FakeBot()
    backend, _ = make(bot=bot)
    seen: list[Any] = []

    async def listener(event: Any) -> None:
        seen.append(event)

    backend.subscribe(listener)

    asyncio.run(
        bot.listeners["on_wavelink_track_end"](event_payload(FakePlayer(1, 10), reason=reason))
    )

    assert seen == []


def test_a_stuck_track_is_logged_as_a_warning_and_an_ordinary_end_is_not(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = FakeBot()
    make(bot=bot)

    with caplog.at_level(logging.INFO, logger="aha.voice"):
        asyncio.run(
            bot.listeners["on_wavelink_track_stuck"](
                event_payload(FakePlayer(2, 20), threshold=10000)
            )
        )
        stuck_messages = list(caplog.messages)
        caplog.clear()
        asyncio.run(
            bot.listeners["on_wavelink_track_end"](
                event_payload(FakePlayer(2, 20), reason="finished")
            )
        )

    assert stuck_messages == [
        "Playback stuck in guild 2 for video dQw4w9WgXcQ: no audio for 10000 ms"
    ]
    assert caplog.messages == []
