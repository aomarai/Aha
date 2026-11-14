"""Offline tests for the Fun cog (dice, coin, time, 8-ball, countdown)."""

from __future__ import annotations

import asyncio
import random
import zoneinfo
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import discord
import pytest
from discord.ext import commands

from aha.cogs import fun as fun_module
from aha.cogs.fun import ANSWERS, Fun, format_offset, timezone_names
from aha.cogs.fun import setup as fun_setup
from tests.helpers import Named, StubBot, StubContext, as_bot, as_context, assert_reply, http_error


def test_roll_reports_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    monkeypatch.setattr(random, "randint", lambda a, b: 4)

    callback = cast("Any", cog.roll.callback)
    asyncio.run(callback(cog, as_context(ctx), sides=6))

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.description == "Rolled **4** on a d6."


def test_roll_default_is_d6(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    monkeypatch.setattr(random, "randint", lambda a, b: 3)

    callback = cast("Any", cog.roll.callback)
    asyncio.run(callback(cog, as_context(ctx)))

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.description == "Rolled **3** on a d6."


def test_roll_respects_the_sides_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def spy(a: int, b: int) -> int:
        calls.append((a, b))
        return b

    monkeypatch.setattr(random, "randint", spy)
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    callback = cast("Any", cog.roll.callback)
    asyncio.run(callback(cog, as_context(ctx), sides=20))

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.description == "Rolled **20** on a d20."
    assert calls == [(1, 20)]


def test_roll_rejects_nonpositive_sides() -> None:
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    callback = cast("Any", cog.roll.callback)
    asyncio.run(callback(cog, as_context(ctx), sides=0))

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.description == "`sides` must be at least 1."
    assert embed.color is not None
    assert embed.color.value == 0xED4245  # error red
    assert kwargs["ephemeral"] is True


def test_roll_allows_a_one_sided_die(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lower bound is inclusive: d1 is a degenerate but valid die.
    monkeypatch.setattr(random, "randint", lambda a, b: 1)
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    callback = cast("Any", cog.roll.callback)
    asyncio.run(callback(cog, as_context(ctx), sides=1))

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.description == "Rolled **1** on a d1."


def test_roll_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(random, "randint", lambda a, b: 4)
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext(reject_embeds=True)

    callback = cast("Any", cog.roll.callback)
    asyncio.run(callback(cog, as_context(ctx), sides=6))

    assert_reply(ctx.replies, "Rolled **4** on a d6.")


def test_roll_uses_an_info_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "randint", lambda a, b: 1)
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    callback = cast("Any", cog.roll.callback)
    asyncio.run(callback(cog, as_context(ctx), sides=6))

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.color is not None
    assert embed.color.value == 0x5865F2  # info blurple
    assert embed.footer is not None
    assert embed.footer.text == "Aha"


def test_roll_calls_randint_with_the_right_range(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def spy(a: int, b: int) -> int:
        calls.append((a, b))
        return 4

    monkeypatch.setattr(random, "randint", spy)
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    callback = cast("Any", cog.roll.callback)
    asyncio.run(callback(cog, as_context(ctx), sides=8))

    assert calls == [(1, 8)]


def test_coin_reports_heads_or_tails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "choice", lambda seq: "heads")
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    callback = cast("Any", cog.coin.callback)
    asyncio.run(callback(cog, as_context(ctx)))

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.description == "Heads!"


def test_coin_reports_tails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "choice", lambda seq: "tails")
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    callback = cast("Any", cog.coin.callback)
    asyncio.run(callback(cog, as_context(ctx)))

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.description == "Tails!"


def test_coin_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(random, "choice", lambda seq: "heads")
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext(reject_embeds=True)

    callback = cast("Any", cog.coin.callback)
    asyncio.run(callback(cog, as_context(ctx)))

    assert_reply(ctx.replies, "Heads!")


def test_coin_uses_an_info_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "choice", lambda seq: "tails")
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    callback = cast("Any", cog.coin.callback)
    asyncio.run(callback(cog, as_context(ctx)))

    content, kwargs = ctx.replies[0]
    assert content is None
    embed = kwargs["embed"]
    assert embed.color is not None
    assert embed.color.value == 0x5865F2  # info blurple
    assert embed.footer is not None
    assert embed.footer.text == "Aha"


def test_coin_calls_choice_with_heads_and_tails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def spy(seq: tuple[str, ...]) -> str:
        calls.append(seq)
        return "heads"

    monkeypatch.setattr(random, "choice", spy)
    bot = StubBot()
    cog = Fun(as_bot(bot))
    ctx = StubContext()

    callback = cast("Any", cog.coin.callback)
    asyncio.run(callback(cog, as_context(ctx)))

    assert calls == [("heads", "tails")]


def test_command_metadata_matches_the_public_surface() -> None:
    # A rename or description loss changes the slash-command surface users see,
    # so it must fail the suite even though every behaviour test still passes.
    assert Fun.roll.qualified_name == "roll"
    assert Fun.roll.description == "Roll a die."
    assert Fun.coin.qualified_name == "coin"
    assert Fun.coin.description == "Flip a coin."


def test_roll_and_coin_are_rate_limited_per_user() -> None:
    for command in (Fun.roll, Fun.coin, Fun.time_in, Fun.eight_ball, Fun.countdown):
        cooldown = command.cooldown
        assert cooldown is not None
        assert (cooldown.rate, cooldown.per) == (1, 5)
        assert command._buckets.type is commands.BucketType.user


def test_setup_registers_the_fun_cog() -> None:
    bot = StubBot()

    asyncio.run(fun_setup(as_bot(bot)))

    assert len(bot.added_cogs) == 1
    cog = bot.added_cogs[0]
    assert isinstance(cog, Fun)
    assert cast("Any", cog.bot) is bot


#: A fixed "now" for the time commands: Friday 13 November 2026, 18:42:11 UTC.
_NOW = datetime(2026, 11, 13, 18, 42, 11, tzinfo=UTC)


def _fixed_clock(monkeypatch: pytest.MonkeyPatch, now: datetime = _NOW) -> None:
    monkeypatch.setattr(fun_module, "utcnow", lambda: now)


def _call(cog: Fun, name: str, ctx: StubContext, *args: Any, **kwargs: Any) -> None:
    callback = cast("Any", getattr(cog, name).callback)
    asyncio.run(callback(cog, as_context(ctx), *args, **kwargs))


def _description(ctx: StubContext) -> str:
    ((_, kwargs),) = ctx.replies
    return str(kwargs["embed"].description)


def test_time_defaults_to_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "time_in", ctx)

    assert _description(ctx) == "**UTC**\nFriday 13 November 2026, 18:42:11 UTC (UTC+00:00)"


def test_time_converts_to_the_requested_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "time_in", ctx, tz="Europe/Berlin")

    assert _description(ctx) == (
        "**Europe/Berlin**\nFriday 13 November 2026, 19:42:11 CET (UTC+01:00)"
    )


def test_time_matches_zone_names_case_insensitively_and_shows_the_canonical_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "time_in", ctx, tz="  america/NEW_york ")

    assert _description(ctx).startswith(
        "**America/New_York**\nFriday 13 November 2026, 13:42:11 EST"
    )
    assert _description(ctx).endswith("(UTC-05:00)")


def test_time_handles_a_half_hour_offset_and_a_date_change(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch, datetime(2026, 11, 13, 20, 30, tzinfo=UTC))
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "time_in", ctx, tz="Asia/Kolkata")

    assert (
        _description(ctx) == "**Asia/Kolkata**\nSaturday 14 November 2026, 02:00:00 IST (UTC+05:30)"
    )


@pytest.mark.parametrize("bad", ["Mars/Olympus", "America", "../etc/passwd", ""])
def test_time_rejects_an_unknown_zone_without_raising(bad: str) -> None:
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "time_in", ctx, tz=bad)

    ((content, kwargs),) = ctx.replies
    assert content is None
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].description == (
        f"`{bad}` is not a timezone I know. Try `Europe/Berlin`."
    )


def test_time_error_echoes_at_most_forty_characters_of_the_input() -> None:
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "time_in", ctx, tz="x" * 300)

    assert _description(ctx) == f"`{'x' * 40}` is not a timezone I know. Try `Europe/Berlin`."


def test_time_error_cannot_be_broken_out_of_the_code_span() -> None:
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "time_in", ctx, tz="`@everyone`")

    # Backticks become apostrophes, so the echo stays inside its code span.
    assert _description(ctx) == "`'@everyone'` is not a timezone I know. Try `Europe/Berlin`."


def test_time_error_does_not_backslash_escape_markdown_inside_the_code_span() -> None:
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "time_in", ctx, tz="Mars_Olympus*")

    assert _description(ctx) == "`Mars_Olympus*` is not a timezone I know. Try `Europe/Berlin`."


def test_time_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext(reject_embeds=True)

    _call(Fun(as_bot(StubBot())), "time_in", ctx)

    assert_reply(ctx.replies, "**UTC**\nFriday 13 November 2026, 18:42:11 UTC (UTC+00:00)")


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "+00:00"),
        (3600, "+01:00"),
        (-18000, "-05:00"),
        (19800, "+05:30"),
        (-12600, "-03:30"),
        (45900, "+12:45"),
    ],
)
def test_format_offset(seconds: int, text: str) -> None:
    assert format_offset(timedelta(seconds=seconds)) == text


def test_format_offset_without_an_offset_is_utc() -> None:
    assert format_offset(None) == "+00:00"


def test_utcnow_is_timezone_aware_and_current() -> None:
    before = datetime.now(UTC)
    now = fun_module.utcnow()

    assert now.tzinfo is UTC
    assert before <= now <= datetime.now(UTC)


def test_timezone_names_map_lowercase_to_the_canonical_spelling() -> None:
    names = timezone_names()

    assert names["europe/berlin"] == "Europe/Berlin"
    assert names["utc"] == "UTC"
    assert all(zoneinfo.ZoneInfo(name) for name in list(names.values())[:5])


def test_time_autocomplete_filters_by_substring_and_caps_the_list() -> None:
    cog = Fun(as_bot(StubBot()))
    callback = cast("Any", Fun.time_zone_autocomplete)  # the plain function, taking the cog

    berlin = asyncio.run(callback(cog, None, "BERLIN"))
    everything = asyncio.run(callback(cog, None, ""))

    assert [(c.name, c.value) for c in berlin] == [("Europe/Berlin", "Europe/Berlin")]
    assert len(everything) == 25
    assert asyncio.run(callback(cog, None, "no-such-zone")) == []


def test_eight_ball_echoes_the_question_and_gives_a_random_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def fake_choice(options: Any) -> Any:
        seen.append(options)
        return options[3]

    monkeypatch.setattr(random, "choice", fake_choice)
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "eight_ball", ctx, question="Will it rain?")

    assert _description(ctx) == "> Will it rain?\n🎱 Yes, definitely."
    assert seen == [ANSWERS]


def test_the_eight_ball_has_the_classic_twenty_answers() -> None:
    assert len(ANSWERS) == 20
    assert len(set(ANSWERS)) == 20
    assert "It is certain." in ANSWERS
    assert "Very doubtful." in ANSWERS


def test_eight_ball_neutralises_markdown_and_mentions_in_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(random, "choice", lambda options: options[0])
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "eight_ball", ctx, question="@everyone **now** `x`?")

    description = _description(ctx)
    assert "@everyone" not in description.replace("@\u200beveryone", "")
    assert "\\*\\*now\\*\\*" in description  # markdown escaped
    assert "`" not in description


def test_eight_ball_shortens_a_very_long_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "choice", lambda options: options[0])
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "eight_ball", ctx, question="q" * 500)

    assert _description(ctx) == "> " + "q" * 199 + "…\n🎱 It is certain."


def test_eight_ball_keeps_a_question_exactly_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "choice", lambda options: options[0])
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "eight_ball", ctx, question="q" * 200)

    assert _description(ctx) == "> " + "q" * 200 + "\n🎱 It is certain."


def test_eight_ball_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(random, "choice", lambda options: options[0])
    ctx = StubContext(reject_embeds=True)

    _call(Fun(as_bot(StubBot())), "eight_ball", ctx, question="Ready?")

    assert_reply(ctx.replies, "> Ready?\n🎱 It is certain.")


def test_countdown_shows_a_relative_and_an_absolute_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()
    stamp = int(datetime(2026, 12, 25, 18, 0, tzinfo=UTC).timestamp())

    _call(Fun(as_bot(StubBot())), "countdown", ctx, "2026-12-25 18:00", label="Christmas")

    assert _description(ctx) == f"**Christmas**: <t:{stamp}:R> (<t:{stamp}:F>)"


def test_countdown_treats_a_bare_date_as_midnight_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()
    stamp = int(datetime(2026, 12, 25, tzinfo=UTC).timestamp())

    _call(Fun(as_bot(StubBot())), "countdown", ctx, "2026-12-25")

    assert _description(ctx) == f"**Countdown**: <t:{stamp}:R> (<t:{stamp}:F>)"


def test_countdown_honours_an_explicit_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()
    stamp = int(datetime(2026, 12, 25, 17, 0, tzinfo=UTC).timestamp())  # 18:00 at +01:00

    _call(Fun(as_bot(StubBot())), "countdown", ctx, "2026-12-25T18:00:00+01:00", label="x")

    assert f"<t:{stamp}:R>" in _description(ctx)


def test_countdown_label_is_neutralised_and_shortened(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "countdown", ctx, "2026-12-25", label="@everyone " + "l" * 300)

    description = _description(ctx)
    assert "@everyone" not in description.replace("@\u200beveryone", "")
    assert description.startswith("**@\u200beveryone " + "l" * 89 + "**: <t:")  # 100 characters


@pytest.mark.parametrize("bad", ["tomorrow", "", "   ", "2026-13-45", "x" * 101])
def test_countdown_rejects_an_unreadable_date(bad: str) -> None:
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "countdown", ctx, bad)

    ((content, kwargs),) = ctx.replies
    assert content is None
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].description == (
        "I couldn't read that date. Use `2026-12-25` or `2026-12-25 18:00`, "
        "optionally with an offset like `+01:00` (otherwise UTC)."
    )


def test_countdown_rejects_a_time_that_has_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "countdown", ctx, "2026-11-13 18:42:11")  # exactly now

    ((_, kwargs),) = ctx.replies
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].description == "That time has already passed."


def test_countdown_accepts_a_moment_one_second_ahead(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()

    _call(Fun(as_bot(StubBot())), "countdown", ctx, "2026-11-13 18:42:12")

    assert "<t:" in _description(ctx)


def test_countdown_falls_back_to_plain_text_when_embed_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext(reject_embeds=True)
    stamp = int(datetime(2026, 12, 25, tzinfo=UTC).timestamp())

    _call(Fun(as_bot(StubBot())), "countdown", ctx, "2026-12-25", label="Xmas")

    assert_reply(ctx.replies, f"**Xmas**: <t:{stamp}:R> (<t:{stamp}:F>)")


def test_new_command_metadata_matches_the_public_surface() -> None:
    assert (Fun.time_in.qualified_name, Fun.time_in.description) == (
        "time",
        "Show the current time in a timezone.",
    )
    assert (Fun.eight_ball.qualified_name, Fun.eight_ball.description) == (
        "8ball",
        "Ask the magic 8-ball a question.",
    )
    assert (Fun.countdown.qualified_name, Fun.countdown.description) == (
        "countdown",
        "Count down to a date.",
    )
    for command in (Fun.time_in, Fun.eight_ball, Fun.countdown):
        assert command.extras["category"] == "Fun"


def test_time_propagates_a_failure_of_the_plain_text_fallback_too() -> None:
    ctx = StubContext()
    ctx.fail_with = http_error()  # every reply fails, including the fallback

    with pytest.raises(Exception, match="boom"):
        _call(Fun(as_bot(StubBot())), "time_in", ctx)


def test_a_backtick_in_a_label_becomes_an_apostrophe(monkeypatch: pytest.MonkeyPatch) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()
    stamp = int(datetime(2026, 12, 25, tzinfo=UTC).timestamp())

    _call(Fun(as_bot(StubBot())), "countdown", ctx, "2026-12-25", label="it`s")

    assert _description(ctx) == f"**it's**: <t:{stamp}:R> (<t:{stamp}:F>)"


def test_countdown_accepts_a_date_of_exactly_the_length_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()
    exactly_100 = "2026-12-25 18:00:00." + "0" * 80

    _call(Fun(as_bot(StubBot())), "countdown", ctx, exactly_100)

    assert len(exactly_100) == 100
    assert "<t:" in _description(ctx)


def test_countdown_rejects_a_date_one_character_over_the_limit_even_if_it_would_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fixed_clock(monkeypatch)
    ctx = StubContext()
    over = "2026-12-25 18:00:00." + "0" * 81  # a valid ISO string, 101 characters long

    _call(Fun(as_bot(StubBot())), "countdown", ctx, over)

    assert len(over) == 101
    assert ctx.replies[0][1]["ephemeral"] is True
    assert "couldn't read that date" in _description(ctx)


def test_roll_declares_native_slash_bounds_and_a_description() -> None:
    parameters = {p.name: p for p in cast("Any", Fun.roll).app_command.parameters}

    sides = parameters["sides"]
    assert (sides.min_value, sides.max_value) == (1, 1_000_000)
    assert sides.description == "How many sides the die has (1 to 1,000,000, default 6)."
    assert sides.required is False


def test_coin_answers_in_the_users_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "choice", lambda seq: "tails")
    cog = Fun(as_bot(StubBot()))
    ctx = StubContext()
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)

    asyncio.run(cast("Any", cog.coin.callback)(cog, as_context(ctx)))

    assert ctx.replies[0][1]["embed"].description == "Zahl!"
