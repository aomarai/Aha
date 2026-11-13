"""Offline tests for the Fun cog (dice and coin)."""

from __future__ import annotations

import asyncio
import random
from typing import Any, cast

import pytest

from aha.cogs.fun import Fun
from aha.cogs.fun import setup as fun_setup
from tests.helpers import StubBot, StubContext, as_bot, as_context


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

    assert ctx.replies == [("Rolled **4** on a d6.", {})]


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

    assert ctx.replies == [("Heads!", {})]


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


def test_setup_registers_the_fun_cog() -> None:
    bot = StubBot()

    asyncio.run(fun_setup(as_bot(bot)))

    assert len(bot.added_cogs) == 1
    cog = bot.added_cogs[0]
    assert isinstance(cog, Fun)
    assert cast("Any", cog.bot) is bot
