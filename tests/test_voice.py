"""Tests for the Lavalink adapter behind voice playback, against a fake node, pool and Discord."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import discord
import pytest
import wavelink

from aha import voice as voice_module
from aha.voice import CONNECT_TIMEOUT, NODE_ID, VoiceError, WavelinkBackend
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


class FakeNode:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.status = wavelink.NodeStatus.CONNECTED
        self.players: dict[int, FakePlayer] = {}


class FakePool:
    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.nodes: dict[str, FakeNode] = {}
        self.connects: list[tuple[list[FakeNode], Any]] = []

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
