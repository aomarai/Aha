"""Offline tests for the paginated help command (no Discord connection)."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from aha.cogs import help as help_module
from aha.cogs.help import Help, HelpPaginator, paginate
from aha.cogs.help import setup as help_setup
from tests.helpers import (
    StubBot,
    StubContext,
    StubInteraction,
    as_bot,
    as_context,
    as_interaction,
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

    assert ctx.replies == [
        ("`/ping` — Check latency.\n`/bare` — No description", {"ephemeral": True})
    ]


def test_help_with_no_visible_commands_says_so() -> None:
    ctx = StubContext()

    _run_help(_cog([stub_command("secret", hidden=True)]), ctx)

    assert ctx.replies == [("No commands available.", {"ephemeral": True})]


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

    assert [s.edited for s in steps] == [
        [{"content": "b\n\nPage 2/3", "view": view}],
        [{"content": "c\n\nPage 3/3", "view": view}],
        [{"content": "b\n\nPage 2/3", "view": view}],
    ]
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
    assert stranger.sent == [("response", "This help menu isn't yours.", {"ephemeral": True})]


def test_setup_registers_the_help_cog() -> None:
    bot = StubBot([])

    asyncio.run(help_setup(as_bot(bot)))

    assert len(bot.added_cogs) == 1
    assert isinstance(bot.added_cogs[0], Help)
    assert cast("Any", bot.added_cogs[0].bot) is bot
