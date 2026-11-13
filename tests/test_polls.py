"""Offline tests for native polls and the Count reactions context menu."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any, cast

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from aha.bot import COG_MODULES, AhaBot
from aha.cogs.polls import (
    MAX_ANSWER_LENGTH,
    MAX_DURATION_HOURS,
    MAX_OPTIONS,
    MAX_QUESTION_LENGTH,
    MIN_OPTIONS,
    Polls,
    parse_options,
)
from aha.cogs.polls import setup as polls_setup
from aha.config import Settings
from tests.helpers import (
    Named,
    StubBot,
    StubContext,
    StubInteraction,
    as_bot,
    as_context,
    as_interaction,
    assert_no_mentions,
)


def _poll(ctx: StubContext, *args: Any, **kwargs: Any) -> None:
    cog = Polls(as_bot(StubBot()))
    callback = cast("Any", cog.poll.callback)
    asyncio.run(callback(cog, as_context(ctx), *args, **kwargs))


def _error(ctx: StubContext) -> str:
    ((content, kwargs),) = ctx.replies
    assert content is None
    assert kwargs["ephemeral"] is True
    assert "poll" not in kwargs
    return str(kwargs["embed"].description)


def test_a_poll_is_created_natively_with_the_given_answers() -> None:
    ctx = StubContext()

    _poll(ctx, "Lunch?", "Pizza, Pasta ,  Sushi", 12, True)

    ((content, kwargs),) = ctx.replies
    assert content is None
    native = kwargs["poll"]
    assert isinstance(native, discord.Poll)
    assert native.question == "Lunch?"
    assert [answer.text for answer in native.answers] == ["Pizza", "Pasta", "Sushi"]
    assert native.duration == timedelta(hours=12)
    assert native.multiple is True
    assert_no_mentions(kwargs["allowed_mentions"])


def test_a_poll_defaults_to_one_vote_for_a_day() -> None:
    ctx = StubContext()

    _poll(ctx, "Q", "a,b")

    native = ctx.replies[0][1]["poll"]
    assert native.duration == timedelta(hours=24)
    assert native.multiple is False


def test_the_question_is_trimmed() -> None:
    ctx = StubContext()

    _poll(ctx, "  Lunch?  ", "a,b")

    assert ctx.replies[0][1]["poll"].question == "Lunch?"


@pytest.mark.parametrize("question", ["", "   ", "q" * (MAX_QUESTION_LENGTH + 1)])
def test_a_missing_or_overlong_question_is_rejected(question: str) -> None:
    ctx = StubContext()

    _poll(ctx, question, "a,b")

    assert _error(ctx) == "The question must be 1 to 300 characters long."


def test_a_question_at_the_length_limit_is_accepted() -> None:
    ctx = StubContext()

    _poll(ctx, "q" * MAX_QUESTION_LENGTH, "a,b")

    assert ctx.replies[0][1]["poll"].question == "q" * MAX_QUESTION_LENGTH


@pytest.mark.parametrize("raw", ["", "only one", "a,", " , a ,", ",,,"])
def test_fewer_than_two_options_are_rejected(raw: str) -> None:
    ctx = StubContext()

    _poll(ctx, "Q", raw)

    assert _error(ctx) == "Give at least 2 options, separated by commas."


def test_more_than_ten_options_are_rejected() -> None:
    ctx = StubContext()

    _poll(ctx, "Q", ",".join(f"o{i}" for i in range(MAX_OPTIONS + 1)))

    assert _error(ctx) == "A poll can have at most 10 options (you gave 11)."


def test_ten_options_are_accepted() -> None:
    ctx = StubContext()

    _poll(ctx, "Q", ",".join(f"o{i}" for i in range(MAX_OPTIONS)))

    assert len(ctx.replies[0][1]["poll"].answers) == MAX_OPTIONS


def test_an_overlong_option_is_rejected_and_one_at_the_limit_is_not() -> None:
    too_long = StubContext()
    at_limit = StubContext()

    _poll(too_long, "Q", f"{'x' * (MAX_ANSWER_LENGTH + 1)}, ok")
    _poll(at_limit, "Q", f"{'x' * MAX_ANSWER_LENGTH}, ok")

    assert _error(too_long) == "Each option can be at most 55 characters."
    assert at_limit.replies[0][1]["poll"].answers[0].text == "x" * MAX_ANSWER_LENGTH


def test_duplicate_options_are_rejected_ignoring_case() -> None:
    ctx = StubContext()

    _poll(ctx, "Q", "Pizza, pizza, Pasta")

    assert _error(ctx) == "Each option must be different."


def test_parse_options_returns_the_list_or_the_reason() -> None:
    assert parse_options("a, b") == ["a", "b"]
    assert parse_options("a") == "Give at least 2 options, separated by commas."
    assert MIN_OPTIONS == 2


def test_the_duration_is_limited_to_what_discord_allows() -> None:
    parameter = cast("Any", Polls.poll.app_command)._params["duration"]

    assert MAX_DURATION_HOURS == 768  # 32 days, Discord's maximum
    assert (parameter.min_value, parameter.max_value) == (1, 768)
    assert parameter.default == 24


def test_poll_metadata_and_cooldown() -> None:
    assert Polls.poll.qualified_name == "poll"
    assert Polls.poll.description == "Start a poll people can vote in."
    assert Polls.poll.extras["category"] == "Fun"
    cooldown = Polls.poll.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 30)
    assert Polls.poll._buckets.type is commands.BucketType.user


def _count(message: Any) -> StubInteraction:
    interaction = StubInteraction()
    cog = Polls(as_bot(StubBot()))
    asyncio.run(cog.count_reactions(as_interaction(interaction), message))
    return interaction


def test_count_reactions_lists_each_reaction_privately() -> None:
    message = Named(reactions=[Named(emoji="👍", count=3), Named(emoji="❤️", count=1)])

    interaction = _count(message)

    ((kind, content, kwargs),) = interaction.sent
    assert (kind, content) == ("response", "👍 3\n❤️ 1")
    assert kwargs["ephemeral"] is True
    assert_no_mentions(kwargs["allowed_mentions"])


def test_count_reactions_says_so_when_there_are_none() -> None:
    interaction = _count(Named(reactions=[]))

    ((_, content, kwargs),) = interaction.sent
    assert content == "There are no reactions on that message."
    assert kwargs["ephemeral"] is True


def test_the_context_menu_follows_the_cogs_lifecycle() -> None:
    bot = AhaBot(Settings(token="test-token"))

    async def scenario() -> tuple[object, object]:
        await bot.add_cog(Polls(bot))
        present = bot.tree.get_command("Count reactions", type=discord.AppCommandType.message)
        await bot.remove_cog("Polls")
        gone = bot.tree.get_command("Count reactions", type=discord.AppCommandType.message)
        return present, gone

    present, gone = asyncio.run(scenario())

    assert isinstance(present, app_commands.ContextMenu)
    assert present.name == "Count reactions"
    assert gone is None


def test_the_registered_menu_runs_the_reaction_count() -> None:
    bot = AhaBot(Settings(token="test-token"))
    interaction = StubInteraction()
    message = Named(reactions=[Named(emoji="🎉", count=2)])

    async def scenario() -> None:
        await bot.add_cog(Polls(bot))
        menu = bot.tree.get_command("Count reactions", type=discord.AppCommandType.message)
        assert isinstance(menu, app_commands.ContextMenu)
        await menu.callback(as_interaction(interaction), cast("Any", message))

    asyncio.run(scenario())

    ((_, content, kwargs),) = interaction.sent
    assert content == "🎉 2"
    assert kwargs["ephemeral"] is True


def test_setup_registers_the_polls_cog() -> None:
    bot = StubBot()

    asyncio.run(polls_setup(as_bot(bot)))

    assert len(bot.added_cogs) == 1
    assert isinstance(bot.added_cogs[0], Polls)


def test_the_polls_cog_is_loaded_at_startup() -> None:
    assert "aha.cogs.polls" in COG_MODULES
