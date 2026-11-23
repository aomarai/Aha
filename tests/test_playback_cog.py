"""Offline tests for the queue commands and buttons, with a fake audio backend and queue store."""

from __future__ import annotations

import asyncio
import re
from typing import Any, cast

import discord
import pytest

from aha.cogs.playback import (
    ACTIONS,
    PAGE_LIMIT,
    ControlButton,
    Playback,
    Result,
    controls,
    format_length,
)
from aha.cogs.playback import setup as playback_setup
from tests.helpers import Named, StubContext, StubInteraction, as_context, as_interaction
from tests.test_voice_cog import FakeAudio, FakeBot, FakeVoiceChannel, ctx_in

GUILD = 11


def make(audio: FakeAudio | None = None, **settings: Any) -> Playback:
    return Playback(cast("Any", FakeBot(audio if audio is not None else FakeAudio(), **settings)))


def queue_tracks(cog: Playback, *requesters: int, playing: bool = True) -> list[Any]:
    """Queue one track per requester and (by default) start the first, in the bot's channel 10."""
    bot = cast("Any", cog.bot)
    bot.audio.players[GUILD] = 10
    entries = [
        asyncio.run(
            bot.store.add(
                GUILD,
                video_id=f"{n:011d}",
                title=f"title {n}",
                length_ms=213_000,
                requester_id=who,
                limit=50,
            )
        )
        for n, who in enumerate(requesters, start=1)
    ]
    if playing:
        asyncio.run(bot.playback.ensure_playing(GUILD))
    return entries


def listener(channel_id: int = 10, *, manager: bool = False, user_id: int = 33) -> StubContext:
    """A context whose author sits in voice channel ``channel_id``."""
    ctx = ctx_in(FakeVoiceChannel(channel_id=channel_id), manage_channels=manager)
    cast("Any", ctx.author).id = user_id
    return ctx


def run(cog: Playback, command: Any, ctx: StubContext, *args: Any) -> None:
    asyncio.run(cast("Any", command.callback)(cog, as_context(ctx), *args))


def text(ctx: StubContext) -> str:
    ((_, kwargs),) = ctx.replies
    return cast("str", kwargs["embed"].description)


def private(ctx: StubContext) -> bool:
    ((_, kwargs),) = ctx.replies
    return bool(kwargs.get("ephemeral", False))


# ----------------------------------------------------------------------------- shared refusals


@pytest.mark.parametrize(
    "command",
    [Playback.skip_command, Playback.pause_command, Playback.stop_command],
)
def test_the_controls_say_playback_is_off_or_unavailable_before_anything_else(command: Any) -> None:
    ctx = listener()
    cog = make()
    cast("Any", cog.bot).audio = None
    run(cog, command, ctx)
    assert text(ctx) == "Voice playback is not available in this deployment."
    assert private(ctx) is True

    ctx = listener()
    cog = make()
    cast("Any", cog.bot).playback = None
    run(cog, command, ctx)
    assert text(ctx).startswith("Voice playback needs a database")

    audio = FakeAudio()
    audio.up = False
    ctx = listener()
    run(make(audio), command, ctx)
    assert text(ctx).startswith("The audio server isn't reachable")


@pytest.mark.parametrize(
    "command",
    [Playback.skip_command, Playback.pause_command, Playback.stop_command],
)
def test_the_controls_need_the_bot_to_be_in_a_voice_channel(command: Any) -> None:
    ctx = listener()

    run(make(), command, ctx)

    assert text(ctx) == "I'm not in a voice channel."
    assert private(ctx) is True


@pytest.mark.parametrize(
    "command",
    [Playback.skip_command, Playback.pause_command, Playback.stop_command],
)
def test_the_controls_need_the_caller_in_the_bots_channel_or_manage_channels(command: Any) -> None:
    cog = make()
    queue_tracks(cog, 33)
    outsider = listener(channel_id=99)
    run(cog, command, outsider)
    assert text(outsider) == "Join my voice channel first, or ask someone with Manage Channels."

    nowhere = ctx_in(None)
    run(cog, command, nowhere)
    assert text(nowhere).startswith("Join my voice channel first")


@pytest.mark.parametrize(
    "command",
    [Playback.skip_command, Playback.pause_command, Playback.stop_command],
)
def test_the_controls_say_when_nothing_is_playing(command: Any) -> None:
    cog = make()
    cast("Any", cog.bot).audio.players[GUILD] = 10
    ctx = listener()

    run(cog, command, ctx)

    assert text(ctx) == "Nothing is playing."


# ----------------------------------------------------------------------------- /pause and /resume


def test_pause_pauses_and_resume_resumes() -> None:
    cog = make()
    queue_tracks(cog, 33)
    audio = cast("Any", cog.bot).audio
    ctx = listener()

    run(cog, Playback.pause_command, ctx)
    assert text(ctx) == "⏸️ Paused." and private(ctx) is False
    assert audio.paused == {GUILD}

    ctx = listener()
    run(cog, Playback.resume_command, ctx)
    assert text(ctx) == "▶️ Resumed."
    assert audio.paused == set()


def test_anyone_in_the_channel_may_pause_not_only_the_requester() -> None:
    cog = make()
    queue_tracks(cog, 7)  # someone else asked for it
    ctx = listener(user_id=33)

    run(cog, Playback.pause_command, ctx)

    assert text(ctx) == "⏸️ Paused."


def test_a_manager_may_control_from_outside_the_channel() -> None:
    cog = make()
    queue_tracks(cog, 7)
    ctx = listener(channel_id=99, manager=True)

    run(cog, Playback.pause_command, ctx)

    assert text(ctx) == "⏸️ Paused."


def test_a_pause_the_node_refuses_is_reported() -> None:
    from aha.voice import VoiceError

    cog = make()
    queue_tracks(cog, 33)
    audio = cast("Any", cog.bot).audio

    async def refuse(guild_id: int, paused: bool) -> None:
        raise VoiceError("voice.play_failed")

    audio.set_paused = refuse
    ctx = listener()

    run(cog, Playback.pause_command, ctx)

    assert text(ctx) == "I couldn't start playback. Try again in a moment."
    assert private(ctx) is True


def test_resume_with_nothing_queued_says_so() -> None:
    cog = make()
    ctx = listener()

    run(cog, Playback.resume_command, ctx)

    assert text(ctx) == "There is nothing to resume."


def test_resume_after_a_restart_rejoins_and_starts_the_saved_queue() -> None:
    audio = FakeAudio()
    cog = make(audio)
    entries = queue_tracks(cog, 33, 34, playing=False)  # the queue survived; the player did not
    audio.players.clear()
    channel = FakeVoiceChannel(channel_id=10, name="General")
    ctx = ctx_in(channel)

    run(cog, Playback.resume_command, ctx)

    assert audio.joined == [channel]
    assert [t.video_id for _, t in audio.plays] == [f"{1:011d}"]
    assert text(ctx).startswith("▶️ Now playing [title 1](https://www.youtube.com/watch?v=")
    assert text(ctx).endswith("(3:33), requested by <@33>.")
    assert ctx.deferred == [False]
    assert ctx.replies[0][1]["view"] is not None
    assert entries[0].id == cast("Any", cog.bot).playback.current_id(GUILD)


def test_resume_after_a_restart_needs_the_caller_in_a_voice_channel() -> None:
    cog = make()
    queue_tracks(cog, 33, playing=False)
    cast("Any", cog.bot).audio.players.clear()
    ctx = ctx_in(None)

    run(cog, Playback.resume_command, ctx)

    assert text(ctx) == "Join a voice channel first, then run this again."


def test_resume_after_a_restart_uses_the_same_join_checks_as_play() -> None:
    cog = make()
    queue_tracks(cog, 33, playing=False)
    cast("Any", cog.bot).audio.players.clear()
    channel = FakeVoiceChannel()
    channel.perms = discord.Permissions.none()
    ctx = ctx_in(channel)

    run(cog, Playback.resume_command, ctx)

    assert text(ctx).startswith("I need Connect and Speak")


def test_resume_when_the_saved_track_cannot_start_says_there_is_nothing_to_resume() -> None:
    from aha.voice import VoiceError

    cog = make()
    queue_tracks(cog, 33, playing=False)
    audio = cast("Any", cog.bot).audio
    audio.players.clear()
    audio.load_error = VoiceError("voice.load_failed")
    ctx = ctx_in(FakeVoiceChannel())

    run(cog, Playback.resume_command, ctx)

    assert text(ctx) == "There is nothing to resume."


def test_resume_reports_a_join_that_fails() -> None:
    from aha.voice import VoiceError

    cog = make()
    queue_tracks(cog, 33, playing=False)
    audio = cast("Any", cog.bot).audio
    audio.players.clear()
    audio.join_error = VoiceError("voice.join_failed")
    ctx = ctx_in(FakeVoiceChannel())

    run(cog, Playback.resume_command, ctx)

    assert text(ctx).startswith("I couldn't join that channel")
    assert private(ctx) is True


def test_resume_without_lavalink_or_a_database() -> None:
    cog = make()
    cast("Any", cog.bot).playback = None
    ctx = listener()
    run(cog, Playback.resume_command, ctx)
    assert text(ctx).startswith("Voice playback needs a database")

    cog = make()
    cast("Any", cog.bot).audio = None
    ctx = listener()
    run(cog, Playback.resume_command, ctx)
    assert text(ctx) == "Voice playback is not available in this deployment."


# ----------------------------------------------------------------------------- /skip and /stop


def test_the_requester_may_skip_their_own_track_and_the_next_one_starts() -> None:
    cog = make()
    queue_tracks(cog, 33, 34)
    audio = cast("Any", cog.bot).audio
    ctx = listener(user_id=33)

    run(cog, Playback.skip_command, ctx)

    assert text(ctx) == (
        "⏭️ Skipped. Now playing [title 2](https://www.youtube.com/watch?v=" + f"{2:011d}" + ")."
    )
    assert [t.video_id for _, t in audio.plays] == [f"{1:011d}", f"{2:011d}"]
    assert private(ctx) is False


def test_skipping_the_last_track_says_the_queue_is_empty() -> None:
    cog = make()
    queue_tracks(cog, 33)
    ctx = listener(user_id=33)

    run(cog, Playback.skip_command, ctx)

    assert text(ctx) == "⏭️ Skipped. The queue is empty."


def test_someone_else_cannot_skip_a_track_they_did_not_queue() -> None:
    cog = make()
    queue_tracks(cog, 7, 34)
    audio = cast("Any", cog.bot).audio
    ctx = listener(user_id=33)

    run(cog, Playback.skip_command, ctx)

    assert text(ctx) == (
        "Only the person who queued that track, or someone with Manage Channels, can do that."
    )
    assert private(ctx) is True
    assert len(audio.plays) == 1


def test_a_manager_may_skip_anyones_track() -> None:
    cog = make()
    queue_tracks(cog, 7, 34)
    ctx = listener(user_id=33, manager=True)

    run(cog, Playback.skip_command, ctx)

    assert text(ctx).startswith("⏭️ Skipped. Now playing")


def test_stop_clears_the_queue_and_leaves_for_the_requester_of_the_playing_track() -> None:
    cog = make()
    queue_tracks(cog, 33, 34, 35)
    audio = cast("Any", cog.bot).audio
    ctx = listener(user_id=33)

    run(cog, Playback.stop_command, ctx)

    assert text(ctx) == "⏹️ Stopped and left the channel. Cleared 3 track(s)."
    assert audio.left == [GUILD]
    assert cast("Any", cog.bot).store.rows == []


def test_stop_is_refused_for_someone_who_did_not_queue_the_playing_track() -> None:
    cog = make()
    queue_tracks(cog, 7, 33)  # 33 queued the second, not the one playing
    audio = cast("Any", cog.bot).audio
    ctx = listener(user_id=33)

    run(cog, Playback.stop_command, ctx)

    assert text(ctx).startswith("Only the person who queued that track")
    assert audio.left == [] and len(cast("Any", cog.bot).store.rows) == 2


def test_a_manager_may_stop_anything() -> None:
    cog = make()
    queue_tracks(cog, 7)
    ctx = listener(user_id=33, manager=True)

    run(cog, Playback.stop_command, ctx)

    assert text(ctx).startswith("⏹️ Stopped")


# ----------------------------------------------------------------------------- /remove


def test_the_requester_may_remove_their_waiting_track() -> None:
    cog = make()
    queue_tracks(cog, 7, 33, 34)
    ctx = listener(user_id=33)

    run(cog, Playback.remove_command, ctx, 2)

    assert text(ctx) == (
        "Removed [title 2](https://www.youtube.com/watch?v=" + f"{2:011d}" + ") from the queue."
    )
    assert [r.requester_id for r in cast("Any", cog.bot).store.rows] == [7, 34]


@pytest.mark.parametrize("position", [-1, 0, 1, 4, 99])
def test_only_waiting_positions_can_be_removed(position: int) -> None:
    cog = make()
    queue_tracks(cog, 33, 33, 33)
    ctx = listener(user_id=33)

    run(cog, Playback.remove_command, ctx, position)

    assert text(ctx) == "There is no waiting track with that number. Use `/queue` to see them."
    assert len(cast("Any", cog.bot).store.rows) == 3


def test_someone_else_cannot_remove_a_track_they_did_not_queue() -> None:
    cog = make()
    queue_tracks(cog, 33, 7)
    ctx = listener(user_id=33)

    run(cog, Playback.remove_command, ctx, 2)

    assert text(ctx).startswith("Only the person who queued that track")
    assert len(cast("Any", cog.bot).store.rows) == 2


def test_a_manager_may_remove_any_waiting_track() -> None:
    cog = make()
    queue_tracks(cog, 7, 8)
    ctx = listener(user_id=33, manager=True)

    run(cog, Playback.remove_command, ctx, 2)

    assert text(ctx).startswith("Removed")


def test_a_track_that_started_meanwhile_is_not_removed() -> None:
    cog = make()
    entries = queue_tracks(cog, 33, 33)
    playback = cast("Any", cog.bot).playback
    original = playback.remove

    async def raced(guild_id: int, entry_id: int) -> Any:
        await original(guild_id, entries[1].id)  # someone removed it first
        return await original(guild_id, entry_id)

    playback.remove = raced
    ctx = listener(user_id=33)

    run(cog, Playback.remove_command, ctx, 2)

    assert text(ctx).startswith("There is no waiting track with that number")


def test_remove_needs_the_same_rights_as_the_other_controls() -> None:
    cog = make()
    ctx = listener()
    run(cog, Playback.remove_command, ctx, 2)
    assert text(ctx) == "I'm not in a voice channel."


# ----------------------------------------------------------------------------- /queue


def test_the_queue_lists_the_playing_track_first_then_the_waiting_ones_privately() -> None:
    cog = make()
    queue_tracks(cog, 33, 34)
    ctx = listener()

    run(cog, Playback.queue_command, ctx)

    ((_, kwargs),) = ctx.replies
    (embed,) = kwargs["embeds"]
    lines = embed.description.split("\n")
    assert lines[0].startswith("▶️ [title 1](https://www.youtube.com/watch?v=")
    assert lines[0].endswith("(3:33) · <@33>")
    assert lines[1].startswith("2. [title 2](") and lines[1].endswith("(3:33) · <@34>")
    assert kwargs["ephemeral"] is True


def test_an_empty_queue_says_so() -> None:
    ctx = listener()

    run(make(), Playback.queue_command, ctx)

    assert text(ctx) == "The queue is empty. Add something with `/play`."
    assert private(ctx) is True


def test_a_long_queue_is_split_into_pages() -> None:
    cog = make()
    bot = cast("Any", cog.bot)
    bot.audio.players[GUILD] = 10
    for n in range(40):
        asyncio.run(
            bot.store.add(
                GUILD,
                video_id=f"{n:011d}",
                title="t" * 100,
                length_ms=1000,
                requester_id=7,
                limit=50,
            )
        )
    ctx = listener()

    run(cog, Playback.queue_command, ctx)

    ((_, kwargs),) = ctx.replies
    pages = [embed.description for embed in kwargs["embeds"]]
    assert len(pages) > 1 and all(len(page) <= PAGE_LIMIT for page in pages)
    assert sum(page.count("\n") + 1 for page in pages) == 40


def test_the_queue_without_a_database_says_so() -> None:
    cog = make()
    cast("Any", cog.bot).playback = None
    ctx = listener()

    run(cog, Playback.queue_command, ctx)

    assert text(ctx).startswith("Voice playback needs a database")


# ----------------------------------------------------------------------------- /nowplaying


def test_now_playing_shows_the_track_its_position_and_who_asked() -> None:
    cog = make()
    queue_tracks(cog, 33)
    ctx = listener()

    run(cog, Playback.nowplaying_command, ctx)

    assert text(ctx).startswith("▶️ [title 1](https://www.youtube.com/watch?v=")
    assert text(ctx).endswith("(1:02 / 3:33), requested by <@33>. Playing.")
    assert private(ctx) is True


def test_now_playing_says_when_paused() -> None:
    cog = make()
    queue_tracks(cog, 33)
    cast("Any", cog.bot).audio.paused.add(GUILD)
    ctx = listener()

    run(cog, Playback.nowplaying_command, ctx)

    assert text(ctx).endswith("Paused.")


def test_now_playing_with_nothing_playing_or_no_voice() -> None:
    ctx = listener()
    run(make(), Playback.nowplaying_command, ctx)
    assert text(ctx) == "Nothing is playing."

    cog = make()
    cast("Any", cog.bot).audio = None
    ctx = listener()
    run(cog, Playback.nowplaying_command, ctx)
    assert text(ctx) == "Voice playback is not available in this deployment."


# ----------------------------------------------------------------------------- the buttons


def test_the_button_template_matches_only_known_actions_and_numeric_guilds() -> None:
    template = ControlButton.__discord_ui_compiled_template__.pattern

    assert re.fullmatch(template, "aha:voice:pause:11")
    assert re.fullmatch(template, "aha:voice:skip:11")
    assert re.fullmatch(template, "aha:voice:stop:11")
    assert not re.fullmatch(template, "aha:voice:nuke:11")
    assert not re.fullmatch(template, "aha:voice:skip:abc")
    assert not re.fullmatch(template, "aha:voice:skip:11:extra")


def test_a_button_is_rebuilt_from_its_custom_id_alone() -> None:
    template = ControlButton.__discord_ui_compiled_template__.pattern
    match = re.fullmatch(template, "aha:voice:skip:11")
    assert match is not None

    button = asyncio.run(
        ControlButton.from_custom_id(as_interaction(StubInteraction()), cast("Any", Named()), match)
    )

    assert (button.action, button.guild_id) == ("skip", 11)
    assert button.item.custom_id == "aha:voice:skip:11"


def test_the_controls_have_a_pause_skip_and_stop_button_that_never_expire() -> None:
    view = controls(GUILD, "en-US")

    labels = [cast("Any", child).item.label for child in view.children]
    ids = [cast("Any", child).item.custom_id for child in view.children]
    styles = [cast("Any", child).item.style for child in view.children]
    assert labels == ["⏯ Pause / resume", "⏭ Skip", "⏹ Stop"]
    assert (
        ids
        == [f"aha:voice:{action}:{GUILD}" for action in ACTIONS]
        == [
            "aha:voice:pause:11",
            "aha:voice:skip:11",
            "aha:voice:stop:11",
        ]
    )
    assert styles == [
        discord.ButtonStyle.secondary,
        discord.ButtonStyle.primary,
        discord.ButtonStyle.danger,
    ]
    assert view.timeout is None


def test_the_buttons_are_labelled_in_the_users_language() -> None:
    labels = [cast("Any", c).item.label for c in controls(GUILD, "de").children]

    assert labels == ["⏯ Pause / Weiter", "⏭ Überspringen", "⏹ Stopp"]


def click(
    button: ControlButton, cog: Playback | None, *, user_id: int = 33, channel: int = 10
) -> StubInteraction:
    interaction = StubInteraction(user_id=user_id)
    interaction.guild_id = GUILD
    interaction.user = Named(
        id=user_id,
        voice=Named(channel=FakeVoiceChannel(channel_id=channel)),
        guild_permissions=discord.Permissions.none(),
    )
    bot = Named(get_cog=lambda name: cog if name == "Playback" else None)
    interaction.client = bot
    asyncio.run(button.callback(as_interaction(interaction)))
    return interaction


def reply_text(interaction: StubInteraction) -> tuple[str, bool]:
    ((_, _, kwargs),) = interaction.sent
    return kwargs["embed"].description, kwargs["ephemeral"]


def test_pressing_pause_toggles_and_answers_privately() -> None:
    cog = make()
    queue_tracks(cog, 33)
    audio = cast("Any", cog.bot).audio

    first = click(ControlButton("pause", GUILD), cog)
    second = click(ControlButton("pause", GUILD), cog)

    assert reply_text(first) == ("⏸️ Paused.", True)
    assert reply_text(second) == ("▶️ Resumed.", True)
    assert audio.paused == set()


def test_pressing_skip_and_stop_apply_the_same_rules_as_the_commands() -> None:
    cog = make()
    queue_tracks(cog, 7, 33)

    refused = click(ControlButton("skip", GUILD), cog, user_id=33)
    assert reply_text(refused)[0].startswith("Only the person who queued that track")
    assert cast("Any", cog.bot).audio.stops == []

    allowed = click(ControlButton("skip", GUILD), cog, user_id=7)
    assert reply_text(allowed)[0].startswith("⏭️ Skipped. Now playing")

    stopped = click(
        ControlButton("stop", GUILD), cog, user_id=33
    )  # the playing track is theirs now
    assert reply_text(stopped)[0].startswith("⏹️ Stopped")


def test_a_press_from_outside_the_channel_is_refused() -> None:
    cog = make()
    queue_tracks(cog, 33)

    outsider = click(ControlButton("pause", GUILD), cog, channel=99)

    assert reply_text(outsider) == (
        "Join my voice channel first, or ask someone with Manage Channels.",
        True,
    )


def test_a_press_when_the_cog_is_not_loaded_or_for_another_guild_is_answered_not_crashed() -> None:
    answered = click(ControlButton("pause", GUILD), None)
    assert reply_text(answered) == ("Voice playback is not available in this deployment.", True)

    cog = make()
    queue_tracks(cog, 33)
    interaction = StubInteraction()
    interaction.guild_id = 999  # a button from another server's message
    interaction.client = Named(get_cog=lambda name: cog)
    asyncio.run(ControlButton("pause", GUILD).callback(as_interaction(interaction)))
    assert reply_text(interaction)[1] is True
    assert cast("Any", cog.bot).audio.paused == set()


def test_press_dispatches_by_action() -> None:
    cog = make()
    queue_tracks(cog, 33)
    result = asyncio.run(
        cog.press(Named(id=33, voice=Named(channel=FakeVoiceChannel())), GUILD, "pause", "en-US")
    )
    assert result == Result(True, "⏸️ Paused.")


# ----------------------------------------------------------------------------- misc


def test_setup_registers_the_persistent_buttons_and_adds_the_cog() -> None:
    added: list[Any] = []
    items: list[Any] = []

    class Bot:
        def add_dynamic_items(self, *args: Any) -> None:
            items.extend(args)

        async def add_cog(self, cog: Any) -> None:
            added.append(cog)

    asyncio.run(playback_setup(cast("Any", Bot())))

    assert items == [ControlButton]
    assert isinstance(added[0], Playback)


def test_the_commands_have_names_a_category_and_a_cooldown() -> None:
    expected = {
        Playback.skip_command: ("skip", 3),
        Playback.pause_command: ("pause", 3),
        Playback.stop_command: ("stop", 3),
        Playback.resume_command: ("resume", 3),
        Playback.queue_command: ("queue", 5),
        Playback.nowplaying_command: ("nowplaying", 5),
        Playback.remove_command: ("remove", 3),
    }
    for command, (name, per) in expected.items():
        assert command.qualified_name == name
        assert command.extras["category"] == "Fun"
        cooldown = command.cooldown
        assert cooldown is not None and (cooldown.rate, cooldown.per) == (1, per)


def test_track_lengths_are_formatted_as_minutes_or_with_hours() -> None:
    assert [format_length(ms) for ms in (0, 62_000, 213_000, 3_723_000)] == [
        "0:00",
        "1:02",
        "3:33",
        "1:02:03",
    ]


# ----------------------------------------------------------------------------- edges


@pytest.mark.parametrize(
    "command",
    [
        Playback.skip_command,
        Playback.pause_command,
        Playback.stop_command,
    ],
)
def test_a_control_used_outside_a_server_does_nothing(command: Any) -> None:
    ctx = StubContext(guild=False)

    run(make(), command, ctx)

    assert ctx.replies == []


def test_remove_outside_a_server_does_nothing() -> None:
    ctx = StubContext(guild=False)

    run(make(), Playback.remove_command, ctx, 2)

    assert ctx.replies == []


def test_resume_outside_a_server_does_nothing() -> None:
    ctx = StubContext(guild=False)

    run(make(), Playback.resume_command, ctx)

    assert ctx.replies == []


def test_resume_in_the_channel_the_bot_is_already_in_does_not_join_again() -> None:
    audio = FakeAudio()
    cog = make(audio)
    queue_tracks(cog, 33, playing=False)  # connected to channel 10, but nothing is playing
    ctx = ctx_in(FakeVoiceChannel(channel_id=10))

    run(cog, Playback.resume_command, ctx)

    assert audio.joined == []
    assert [t.video_id for _, t in audio.plays] == [f"{1:011d}"]


# ------------------------------------------------------------------------ the details


def german(ctx: StubContext) -> StubContext:
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)
    return ctx


def result_of(call: Any) -> Result:
    return cast("Result", asyncio.run(call))


MEMBER = Named(id=33, voice=Named(channel=FakeVoiceChannel(channel_id=10)))


def test_every_refusal_is_a_failed_result_with_its_text_in_both_languages() -> None:
    off = make()
    cast("Any", off.bot).audio = None
    no_db = make()
    cast("Any", no_db.bot).playback = None
    down = make()
    cast("Any", down.bot).audio.up = False
    idle = make()
    cast("Any", idle.bot).audio.players[GUILD] = 10

    cases = [
        (off, "Voice playback is not available in this deployment.", "Sprachwiedergabe ist"),
        (no_db, "Voice playback needs a database", "Die Sprachwiedergabe braucht"),
        (down, "The audio server isn't reachable", "Der Audioserver ist gerade nicht"),
        (make(), "I'm not in a voice channel.", "Ich bin in keinem Sprachkanal."),
        (idle, "Nothing is playing.", "Es läuft nichts."),
    ]
    for cog, english, german_text in cases:
        in_english = result_of(cog.skip(MEMBER, GUILD, "en-US"))
        in_german = result_of(cog.skip(MEMBER, GUILD, "de"))
        assert in_english.ok is False and in_german.ok is False
        assert in_english.text.startswith(english)
        assert in_german.text.startswith(german_text)


def test_a_member_outside_the_channel_is_refused_in_the_users_language() -> None:
    cog = make()
    queue_tracks(cog, 33)
    outsider = Named(id=33, voice=Named(channel=FakeVoiceChannel(channel_id=99)))

    english = result_of(cog.skip(outsider, GUILD, "en-US"))
    german_text = result_of(cog.skip(outsider, GUILD, "de"))

    assert english == Result(
        False, "Join my voice channel first, or ask someone with Manage Channels."
    )
    assert german_text.ok is False and german_text.text.startswith(
        "Tritt zuerst meinem Sprachkanal"
    )


def test_the_playing_state_check_needs_both_a_queue_and_a_playing_track() -> None:
    cog = make()
    audio = cast("Any", cog.bot).audio
    audio.players[GUILD] = 10
    audio.playing.add(GUILD)  # something plays, but nothing is queued here

    assert result_of(cog.skip(MEMBER, GUILD, "en-US")) == Result(False, "Nothing is playing.")

    queue_tracks(cog, 33, playing=False)
    audio.playing.clear()  # queued, but nothing plays
    assert result_of(cog.skip(MEMBER, GUILD, "en-US")) == Result(False, "Nothing is playing.")


def test_pausing_again_keeps_it_paused_and_toggling_flips_it() -> None:
    cog = make()
    queue_tracks(cog, 33)
    audio = cast("Any", cog.bot).audio
    audio.paused.add(GUILD)

    again = result_of(cog.set_paused(MEMBER, GUILD, "en-US", paused=True))
    assert again == Result(True, "⏸️ Paused.") and audio.paused == {GUILD}

    toggled = result_of(cog.set_paused(MEMBER, GUILD, "en-US", paused=None))
    assert toggled == Result(True, "▶️ Resumed.") and audio.paused == set()
    toggled_back = result_of(cog.set_paused(MEMBER, GUILD, "de", paused=None))
    assert toggled_back == Result(True, "⏸️ Pausiert.")


def test_a_refused_pause_is_a_failed_result_in_the_users_language() -> None:
    from aha.voice import VoiceError

    cog = make()
    queue_tracks(cog, 33)

    async def refuse(guild_id: int, paused: bool) -> None:
        raise VoiceError("voice.play_failed")

    cast("Any", cog.bot).audio.set_paused = refuse

    english = result_of(cog.set_paused(MEMBER, GUILD, "en-US", paused=True))
    german_text = result_of(cog.set_paused(MEMBER, GUILD, "de", paused=True))

    assert english == Result(False, "I couldn't start playback. Try again in a moment.")
    assert german_text.ok is False and german_text.text.startswith(
        "Ich konnte die Wiedergabe nicht"
    )


def test_skip_and_stop_results_in_both_languages() -> None:
    cog = make()
    queue_tracks(cog, 7)  # someone else's
    stranger = Named(id=33, voice=Named(channel=FakeVoiceChannel(channel_id=10)))
    for method in (cog.skip, cog.stop):
        english = result_of(method(stranger, GUILD, "en-US"))
        german_text = result_of(method(stranger, GUILD, "de"))
        assert english.ok is False and english.text.startswith("Only the person who queued")
        assert german_text.ok is False and german_text.text.startswith("Das darf nur")

    owner = Named(id=7, voice=Named(channel=FakeVoiceChannel(channel_id=10)))
    emptied = result_of(cog.skip(owner, GUILD, "de"))
    assert emptied == Result(True, "⏭️ Übersprungen. Die Warteschlange ist leer.")

    cog = make()
    queue_tracks(cog, 7, 8)
    next_up = result_of(cog.skip(owner, GUILD, "de"))
    assert next_up.ok is True and next_up.text.startswith("⏭️ Übersprungen. Jetzt läuft [title 2]")
    stopped = result_of(cog.stop(Named(id=8, voice=cast("Any", owner).voice), GUILD, "de"))
    assert stopped == Result(True, "⏹️ Gestoppt und den Kanal verlassen. 1 Titel gelöscht.")


def test_a_member_without_an_id_is_not_the_requester() -> None:
    cog = make()
    entries = queue_tracks(cog, 7)
    nobody = Named(voice=Named(channel=FakeVoiceChannel(channel_id=10)))

    assert cog._may_change(nobody, entries[0]) is False


def test_the_cog_remembers_its_bot() -> None:
    added: list[Any] = []

    class Bot:
        def add_dynamic_items(self, *args: Any) -> None:
            pass

        async def add_cog(self, cog: Any) -> None:
            added.append(cog)

    bot = Bot()
    asyncio.run(playback_setup(cast("Any", bot)))

    assert added[0].bot is bot


def test_results_choose_a_green_or_red_card() -> None:
    from aha import embeds

    ok_ctx, bad_ctx = listener(), listener()
    asyncio.run(Playback._say(as_context(ok_ctx), Result(True, "fine")))
    asyncio.run(Playback._say(as_context(bad_ctx), Result(False, "nope")))

    ((_, ok_kwargs),) = ok_ctx.replies
    ((_, bad_kwargs),) = bad_ctx.replies
    assert ok_kwargs["embed"].colour == embeds.success("x").colour
    assert bad_kwargs["embed"].colour == embeds.error("x").colour
    assert ok_kwargs["ephemeral"] is False and bad_kwargs["ephemeral"] is True


def test_a_button_answer_is_a_green_or_red_card_in_the_users_language() -> None:
    from aha import embeds

    cog = make()
    queue_tracks(cog, 33)
    worked = click(ControlButton("pause", GUILD), cog)
    refused = click(ControlButton("pause", GUILD), cog, channel=99)

    ((_, _, ok_kwargs),) = worked.sent
    ((_, _, bad_kwargs),) = refused.sent
    assert ok_kwargs["embed"].colour == embeds.success("x").colour
    assert bad_kwargs["embed"].colour == embeds.error("x").colour

    interaction = StubInteraction()
    interaction.locale = discord.Locale.german
    interaction.guild_id = GUILD
    interaction.client = Named(get_cog=lambda name: None)
    asyncio.run(ControlButton("pause", GUILD).callback(as_interaction(interaction)))
    assert reply_text(interaction)[0].startswith("Sprachwiedergabe ist")
