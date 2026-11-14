"""Tests for the /url command."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import discord
from discord.ext import commands

from aha.bot import COG_MODULES
from aha.cogs.web import CACHE_CAPACITY, CACHE_TTL, Web
from aha.cogs.web import setup as web_setup
from aha.net import FetchError, Preview, TTLCache
from tests.helpers import Named, StubBot, StubContext, as_bot, as_context
from tests.test_helpers_textutil import ZWSP

_PAGE = Preview(
    url="https://example.com/post",
    title="A Title",
    description="A description.",
    site_name="Example",
)


class _Fetcher:
    """Records the links fetched and answers with a preview or an error."""

    def __init__(self, result: Preview | FetchError = _PAGE) -> None:
        self.result = result
        self.fetched: list[str] = []

    async def __call__(self, url: str) -> Preview:
        self.fetched.append(url)
        if isinstance(self.result, FetchError):
            raise self.result
        return self.result


def _run(cog: Web, ctx: StubContext, link: str) -> None:
    callback = cast("Any", cog.url.callback)
    asyncio.run(callback(cog, as_context(ctx), link))


def _cog(fetcher: _Fetcher, cache: TTLCache[Preview] | None = None) -> Web:
    return Web(as_bot(StubBot()), fetch=fetcher, cache=cache)


def test_a_link_is_previewed() -> None:
    fetcher = _Fetcher()
    ctx = StubContext()

    _run(_cog(fetcher), ctx, "https://example.com/post")

    assert fetcher.fetched == ["https://example.com/post"]
    assert ctx.deferred == [False]  # a fetch can take a moment: acknowledge first, publicly
    ((content, kwargs),) = ctx.replies
    assert content is None
    embed = kwargs["embed"]
    assert embed.title == "A Title"
    assert embed.description == "A description."
    assert [(f.name, f.value, f.inline) for f in embed.fields] == [
        ("Site", "Example", True),
        ("Link", "https://example.com/post", False),
    ]


def test_the_normalised_link_is_what_gets_fetched() -> None:
    fetcher = _Fetcher()

    _run(_cog(fetcher), StubContext(), "  HTTPS://Example.COM/post  ")

    assert fetcher.fetched == ["https://example.com/post"]


def test_a_repeated_link_is_served_from_the_cache_without_fetching_or_deferring() -> None:
    fetcher = _Fetcher()
    cog = _cog(fetcher)
    first, second = StubContext(), StubContext()

    _run(cog, first, "https://example.com/post")
    _run(cog, second, "https://example.com/post")

    assert fetcher.fetched == ["https://example.com/post"]
    assert second.deferred == []
    assert second.replies[0][1]["embed"].title == "A Title"


def test_a_link_that_is_not_allowed_is_refused_privately_before_any_fetch() -> None:
    fetcher = _Fetcher()
    ctx = StubContext()

    _run(_cog(fetcher), ctx, "http://169.254.169.254/latest/meta-data/")

    assert fetcher.fetched == []
    assert ctx.deferred == []
    ((_, kwargs),) = ctx.replies
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].description == "That address isn't allowed."


def test_a_failed_fetch_is_explained_and_not_cached() -> None:
    fetcher = _Fetcher(FetchError("The site took too long to answer.", "net.timeout"))
    cog = _cog(fetcher)
    ctx = StubContext()

    _run(cog, ctx, "https://example.com/slow")
    fetcher.result = _PAGE
    again = StubContext()
    _run(cog, again, "https://example.com/slow")

    ((_, kwargs),) = ctx.replies
    assert kwargs["embed"].description == "The site took too long to answer."
    assert "ephemeral" not in kwargs  # a deferred reply cannot be made private after the fact
    assert fetcher.fetched == ["https://example.com/slow"] * 2  # the failure was not remembered
    assert again.replies[0][1]["embed"].title == "A Title"


def test_untrusted_page_text_is_neutralised() -> None:
    preview = Preview(
        url="https://example.com/",
        title="@everyone **bold** [x](http://evil.test)",
        description="`code` and _italic_ @here",
        site_name="*Site*",
    )
    ctx = StubContext()

    _run(_cog(_Fetcher(preview)), ctx, "https://example.com/")

    embed = ctx.replies[0][1]["embed"]
    assert embed.title == f"@{ZWSP}everyone \\*\\*bold\\*\\* \\[x](http://evil.test)"
    assert embed.description == f"'code' and \\_italic\\_ @{ZWSP}here"
    assert embed.fields[0].value == "\\*Site\\*"


def test_a_page_with_no_title_or_description_falls_back_to_its_host() -> None:
    preview = Preview(
        url="https://news.example.com/a", title=None, description=None, site_name=None
    )
    ctx = StubContext()

    _run(_cog(_Fetcher(preview)), ctx, "https://news.example.com/a")

    embed = ctx.replies[0][1]["embed"]
    assert embed.title == "news.example.com"
    assert embed.description == "This page has no description."
    assert embed.fields[0].value == "news.example.com"


def test_a_long_title_and_description_are_shortened() -> None:
    preview = Preview(
        url="https://example.com/", title="t" * 500, description="d" * 500, site_name=None
    )
    ctx = StubContext()

    _run(_cog(_Fetcher(preview)), ctx, "https://example.com/")

    embed = ctx.replies[0][1]["embed"]
    assert embed.title == "t" * 199 + "…"
    assert embed.description == "d" * 299 + "…"


def test_a_title_and_description_at_their_limits_are_kept_whole() -> None:
    preview = Preview(
        url="https://example.com/", title="t" * 200, description="d" * 300, site_name=None
    )
    ctx = StubContext()

    _run(_cog(_Fetcher(preview)), ctx, "https://example.com/")

    embed = ctx.replies[0][1]["embed"]
    assert (embed.title, embed.description) == ("t" * 200, "d" * 300)


def test_the_cache_is_small_and_short_lived_by_default() -> None:
    assert (CACHE_TTL, CACHE_CAPACITY) == (300.0, 256)


def test_a_cog_builds_its_own_cache_when_none_is_given() -> None:
    fetcher = _Fetcher()
    cog = Web(as_bot(StubBot()), fetch=fetcher)

    _run(cog, StubContext(), "https://example.com/a")
    _run(cog, StubContext(), "https://example.com/a")

    assert fetcher.fetched == ["https://example.com/a"]


def test_url_metadata_cooldown_and_category() -> None:
    assert Web.url.qualified_name == "url"
    assert Web.url.description == "Show a preview of a web page."
    assert Web.url.extras["category"] == "Info"
    cooldown = Web.url.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 15)
    assert Web.url._buckets.type is commands.BucketType.user


def test_setup_registers_the_web_cog_and_it_loads_at_startup() -> None:
    bot = StubBot()

    asyncio.run(web_setup(as_bot(bot)))

    assert isinstance(bot.added_cogs[0], Web)
    assert "aha.cogs.web" in COG_MODULES


def test_the_cog_keeps_its_bot_and_uses_the_cache_it_is_given() -> None:
    bot = StubBot()
    cache: TTLCache[Preview] = TTLCache(ttl=CACHE_TTL, capacity=4)
    cache.put("https://example.com/cached", _PAGE)
    fetcher = _Fetcher()
    cog = Web(as_bot(bot), fetch=fetcher, cache=cache)
    ctx = StubContext()

    _run(cog, ctx, "https://example.com/cached")

    assert cast("Any", cog.bot) is bot
    assert fetcher.fetched == []  # served from the supplied cache
    assert ctx.replies[0][1]["embed"].title == "A Title"


def test_setup_gives_the_cog_its_bot() -> None:
    bot = StubBot()

    asyncio.run(web_setup(as_bot(bot)))

    assert cast("Any", bot.added_cogs[0]).bot is bot


# ------------------------------------------------------------------------- German


def _german(ctx: StubContext) -> StubContext:
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)
    return ctx


def test_validation_errors_are_shown_in_the_users_language() -> None:
    cases = [
        ("not a link at all", "Das sieht nicht nach einem gültigen Link aus."),
        ("ftp://example.com/", "Nur Links mit http:// oder https:// werden unterstützt."),
        (
            "https://user:pw@example.com/",
            "Links mit Benutzername oder Passwort werden nicht unterstützt.",
        ),
        (
            "https://example.com:8443/",
            "Nur die üblichen Web-Ports (80 und 443) werden unterstützt.",
        ),
        ("http://127.0.0.1/", "Diese Adresse ist nicht erlaubt."),
    ]
    for link, expected in cases:
        ctx = _german(StubContext())

        _run(_cog(_Fetcher()), ctx, link)

        assert ctx.replies[0][1]["embed"].description == expected, link


def test_fetch_errors_with_values_are_translated_with_their_values() -> None:
    fetcher = _Fetcher(
        FetchError("The site answered with HTTP 503.", "net.http_status", status=503)
    )
    ctx = _german(StubContext())

    _run(_cog(fetcher), ctx, "https://example.com/down")

    assert ctx.replies[0][1]["embed"].description == "Die Seite hat mit HTTP 503 geantwortet."


def test_the_preview_card_is_labelled_in_the_users_language() -> None:
    ctx = _german(StubContext())
    bare = Preview(url="https://example.com/", title="", description="", site_name="")

    _run(_cog(_Fetcher(bare)), ctx, "https://example.com/")

    embed = ctx.replies[0][1]["embed"]
    assert embed.description == "Diese Seite hat keine Beschreibung."
    assert [f.name for f in embed.fields] == ["Seite", "Adresse"]
