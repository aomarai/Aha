"""Offline tests for the paginated help command (no Discord connection)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from discord.ext import commands

from aha import logctx
from aha.cogs import help as help_module
from aha.cogs.help import (
    CATEGORY_ORDER,
    DEFAULT_CATEGORY,
    Help,
    HelpPaginator,
    category_of,
    group_by_category,
    paginate,
    paginate_sections,
)
from aha.cogs.help import setup as help_setup
from tests.helpers import (
    Named,
    StubBot,
    StubContext,
    StubInteraction,
    as_bot,
    as_context,
    as_interaction,
    assert_no_mentions,
    assert_reply,
    stub_command,
)


def _cog(cmds: list[Any]) -> Help:
    return Help(as_bot(StubBot(cmds)))


def _run_help(cog: Help, ctx: StubContext) -> None:
    callback = cast("Any", cog.help.callback)
    asyncio.run(callback(cog, as_context(ctx)))


def test_paginate_packs_lines_up_to_and_including_the_limit() -> None:
    # "aa\nbb" is exactly 5 characters, so it fits; adding "cc" would not.
    assert paginate(["aa", "bb", "cc"], 5) == ["aa\nbb", "cc"]


def test_paginate_splits_when_one_character_over_the_limit() -> None:
    assert paginate(["aa", "bb"], 4) == ["aa", "bb"]


def test_paginate_truncates_a_line_longer_than_the_limit() -> None:
    assert paginate(["abcdefgh", "z"], 5) == ["abcd…", "z"]


def test_paginate_keeps_a_line_exactly_at_the_limit() -> None:
    assert paginate(["abcde"], 5) == ["abcde"]


def test_paginate_of_nothing_is_no_pages() -> None:
    assert paginate([], 5) == []


def test_default_page_limit_leaves_room_under_discords_cap() -> None:
    assert help_module.PAGE_LIMIT == 1900
    assert paginate(["x" * 1901]) == ["x" * 1899 + "…"]


def test_help_lists_visible_commands_privately_without_a_view() -> None:
    ctx = StubContext()
    cog = _cog(
        [
            stub_command("ping", "Check latency."),
            stub_command("secret", "Hidden.", hidden=True),
            stub_command("bare"),
        ]
    )

    _run_help(cog, ctx)

    assert_reply(
        ctx.replies,
        "`/ping` — Check latency.\n`/bare` — No description",
        ephemeral=True,
    )


def test_help_with_no_visible_commands_says_so() -> None:
    ctx = StubContext()

    _run_help(_cog([stub_command("secret", hidden=True)]), ctx)

    assert_reply(ctx.replies, "No commands available.", ephemeral=True)


def test_help_paginates_when_the_list_is_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(help_module, "PAGE_LIMIT", 40)
    ctx = StubContext()
    cog = _cog([stub_command("one", "First command."), stub_command("two", "Second command.")])

    _run_help(cog, ctx)

    ((content, kwargs),) = ctx.replies
    assert content == "`/one` — First command.\n\nPage 1/2"
    assert kwargs["ephemeral"] is True
    view = kwargs["view"]
    assert isinstance(view, HelpPaginator)
    assert view.pages == ["`/one` — First command.", "`/two` — Second command."]
    assert view.owner_id == 33


def test_paginator_starts_on_first_page_with_previous_disabled() -> None:
    async def build() -> HelpPaginator:
        return HelpPaginator(["a", "b", "c"], owner_id=1)

    view = asyncio.run(build())

    assert view.index == 0
    assert view.previous_page.disabled is True
    assert view.next_page.disabled is False
    assert view.timeout == 120


def test_next_and_previous_flip_pages_and_toggle_buttons() -> None:
    async def scenario() -> tuple[HelpPaginator, list[StubInteraction]]:
        view = HelpPaginator(["a", "b", "c"], owner_id=1)
        steps: list[StubInteraction] = []
        for button in (view.next_page, view.next_page, view.previous_page):
            stub = StubInteraction(user_id=1)
            steps.append(stub)
            await button.callback(as_interaction(stub))
        return view, steps

    view, steps = asyncio.run(scenario())

    edited = [step.edited[0] for step in steps]
    assert [entry["content"] for entry in edited] == [
        "b\n\nPage 2/3",
        "c\n\nPage 3/3",
        "b\n\nPage 2/3",
    ]
    assert [entry["view"] for entry in edited] == [view, view, view]
    for entry in edited:
        assert_no_mentions(entry["allowed_mentions"])
    assert view.index == 1
    assert view.previous_page.disabled is False
    assert view.next_page.disabled is False


def test_last_page_disables_next() -> None:
    async def scenario() -> HelpPaginator:
        view = HelpPaginator(["a", "b"], owner_id=1)
        await view.next_page.callback(as_interaction(StubInteraction(user_id=1)))
        return view

    view = asyncio.run(scenario())

    assert view.next_page.disabled is True
    assert view.previous_page.disabled is False


def test_button_clicks_bind_the_log_context_even_for_a_stranger() -> None:
    async def scenario() -> dict[str, object]:
        view = HelpPaginator(["a", "b"], owner_id=1)
        await view.interaction_check(as_interaction(StubInteraction(user_id=2)))
        return dict(logctx.current())

    context = asyncio.run(scenario())

    assert context["user_id"] == 2
    assert context["interaction_id"] == 777
    assert context["guild_id"] == 11


def test_only_the_owner_may_use_the_buttons() -> None:
    async def scenario() -> tuple[bool, bool]:
        view = HelpPaginator(["a", "b"], owner_id=1)
        return (
            await view.interaction_check(as_interaction(StubInteraction(user_id=1))),
            await view.interaction_check(stranger_interaction),
        )

    stranger = StubInteraction(user_id=2)
    stranger_interaction = as_interaction(stranger)

    owner_ok, stranger_ok = asyncio.run(scenario())

    assert owner_ok is True
    assert stranger_ok is False
    ((kind, content, kwargs),) = stranger.sent
    assert (kind, content) == ("response", "This help menu isn't yours.")
    assert kwargs["ephemeral"] is True
    assert_no_mentions(kwargs["allowed_mentions"])


def test_help_has_a_per_user_cooldown() -> None:
    cooldown = Help.help.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (3, 10)
    assert Help.help._buckets.type is commands.BucketType.user


def test_setup_registers_the_help_cog() -> None:
    bot = StubBot([])

    asyncio.run(help_setup(as_bot(bot)))

    assert len(bot.added_cogs) == 1
    assert isinstance(bot.added_cogs[0], Help)
    assert cast("Any", bot.added_cogs[0].bot) is bot


def test_category_comes_from_extras_and_defaults_to_other() -> None:
    assert category_of(stub_command("a", category="Fun")) == "Fun"
    assert category_of(stub_command("a")) == DEFAULT_CATEGORY == "Other"
    assert category_of(Named(extras=None)) == "Other"
    assert category_of(Named(extras={"category": ""})) == "Other"
    assert category_of(Named(extras={"category": 5})) == "5"


def test_group_by_category_orders_known_then_alphabetical_then_other() -> None:
    commands_ = [
        stub_command("o1", "x"),
        stub_command("z1", "x", category="Zeta"),
        stub_command("f1", "x", category="Fun"),
        stub_command("a1", "x", category="Alpha"),
        stub_command("i1", "x", category="Info"),
        stub_command("f2", "x", category="Fun"),
        stub_command("m1", "x", category="Admin"),
    ]

    grouped = group_by_category(commands_)

    assert [name for name, _ in grouped] == ["Info", "Fun", "Admin", "Alpha", "Zeta", "Other"]
    assert dict(grouped)["Fun"] == ["`/f1` — x", "`/f2` — x"]  # registration order kept
    assert CATEGORY_ORDER == ("Info", "Fun", "Admin")


def test_group_by_category_describes_commands_without_a_description() -> None:
    assert group_by_category([stub_command("bare")]) == [("Other", ["`/bare` — No description"])]


def test_one_category_is_listed_flat_without_a_header() -> None:
    sections = [("Fun", ["`/roll` — Roll.", "`/coin` — Flip."])]

    assert paginate_sections(sections) == ["`/roll` — Roll.\n`/coin` — Flip."]


def test_several_categories_get_a_header_each() -> None:
    sections = [("Info", ["`/ping` — Latency."]), ("Fun", ["`/roll` — Roll.", "`/coin` — Flip."])]

    assert paginate_sections(sections) == [
        "**Info**\n`/ping` — Latency.\n**Fun**\n`/roll` — Roll.\n`/coin` — Flip."
    ]


def test_no_sections_means_no_pages() -> None:
    assert paginate_sections([]) == []


def test_sections_without_any_lines_make_no_pages() -> None:
    assert paginate_sections([("Info", []), ("Fun", [])]) == []


def test_a_header_is_never_left_alone_at_the_bottom_of_a_page() -> None:
    sections = [("Info", ["aaaaaaaaaa"]), ("Fun", ["bbbbbbbbbb"])]
    # "**Info**\naaaaaaaaaa" is 19 chars; adding "**Fun**" alone (27) would fit at limit 35, but
    # "**Fun**\nbbbbbbbbbb" (38) would not, so the whole section moves to the next page.
    pages = paginate_sections(sections, limit=35)

    assert pages == ["**Info**\naaaaaaaaaa", "**Fun**\nbbbbbbbbbb"]


def test_a_section_that_continues_on_a_new_page_repeats_its_header() -> None:
    sections = [("Info", ["aaaaaaaaaa", "bbbbbbbbbb", "cccccccccc"]), ("Fun", ["dddddddddd"])]

    pages = paginate_sections(sections, limit=32)

    assert pages == [
        "**Info**\naaaaaaaaaa\nbbbbbbbbbb",
        "**Info** (continued)\ncccccccccc",
        "**Fun**\ndddddddddd",
    ]
    assert all(len(page) <= 32 for page in pages)


def test_a_continued_header_is_not_added_to_a_section_that_starts_a_page() -> None:
    sections = [("Info", ["aaaaaaaaaa"]), ("Fun", ["bbbbbbbbbb", "cccccccccc"])]

    pages = paginate_sections(sections, limit=32)

    assert pages == [
        "**Info**\naaaaaaaaaa",
        "**Fun**\nbbbbbbbbbb\ncccccccccc",
    ]


def test_an_overlong_line_is_truncated_inside_a_section() -> None:
    pages = paginate_sections([("Info", ["x" * 100]), ("Fun", ["y"])], limit=30)

    # Room beside "**Info** (continued)" and its newline is 30 - 20 - 1 = 9 characters; the
    # truncated section (18 chars) and "**Fun**\ny" (9 chars) then share a 28-character page.
    assert pages == ["**Info**\n" + "x" * 8 + "…\n**Fun**\ny"]
    assert all(len(page) <= 30 for page in pages)


def test_every_page_fits_the_limit_whatever_the_line_lengths() -> None:
    sections = [
        ("Info", ["a" * n for n in (5, 40, 7, 90, 3)]),
        ("Fun", ["b" * n for n in (60, 2, 33)]),
        ("Other", ["c" * 200]),
    ]

    for limit in (30, 45, 80, 200):
        pages = paginate_sections(sections, limit=limit)

        assert pages
        assert all(len(page) <= limit for page in pages), limit


def test_help_groups_commands_under_headers_when_there_are_several_categories() -> None:
    ctx = StubContext()
    cog = _cog(
        [
            stub_command("roll", "Roll a die.", category="Fun"),
            stub_command("ping", "Check latency.", category="Info"),
            stub_command("coin", "Flip a coin.", category="Fun"),
        ]
    )

    _run_help(cog, ctx)

    assert_reply(
        ctx.replies,
        "**Info**\n`/ping` — Check latency.\n"
        "**Fun**\n`/roll` — Roll a die.\n`/coin` — Flip a coin.",
        ephemeral=True,
    )


def test_every_real_command_declares_a_known_category() -> None:
    from aha.bot import COG_MODULES, OPERATOR_COG, AhaBot
    from aha.config import Settings

    async def scenario() -> list[tuple[str, str]]:
        bot = AhaBot(Settings(token="test-token", dev_guild_id=1))
        for module in (*COG_MODULES, OPERATOR_COG):
            await bot.load_extension(module)
        return [(c.qualified_name, category_of(c)) for c in bot.walk_commands()]

    found = asyncio.run(scenario())

    assert found  # the walk really saw the commands
    assert {name for name, _ in found} >= {"ping", "about", "help", "roll", "coin", "sync"}
    # A new command without ``extras={"category": ...}`` would silently land in "Other".
    assert [name for name, category in found if category not in CATEGORY_ORDER] == []
