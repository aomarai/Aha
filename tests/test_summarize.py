"""Offline tests for /summarize: every control, with a fake model and a fake usage store."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import discord
import pytest

from aha.bot import AhaBot
from aha.cogs.summarize import EMBED_LIMIT, Summarize
from aha.cogs.summarize import setup as summarize_setup
from aha.config import Settings
from aha.guildsettings import GuildSettings
from aha.summarizer import AnthropicSummarizer, SummarizeError, Summary
from tests.helpers import Named, StubContext, StubInteraction, as_context, as_interaction


class FakeModel:
    def __init__(self, result: Summary | Exception | None = None) -> None:
        self.result = result or Summary("It is short.", 11, 7)
        self.texts: list[str] = []

    async def summarize(self, text: str) -> Summary:
        self.texts.append(text)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeUsage:
    def __init__(self, limit_hit: bool = False) -> None:
        self.limit_hit = limit_hit
        self.calls: list[tuple[str, int, int | None]] = []

    async def consume(self, user_id: int, limit: int) -> int | None:
        self.calls.append(("consume", user_id, limit))
        return None if self.limit_hit else 1

    async def refund(self, user_id: int) -> None:
        self.calls.append(("refund", user_id, None))


class FakeGuilds:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.asked: list[int] = []

    async def get(self, guild_id: int) -> GuildSettings:
        self.asked.append(guild_id)
        return GuildSettings(guild_id, summaries_enabled=self.enabled)


def make(
    *,
    model: FakeModel | None = None,
    usage: FakeUsage | None = None,
    guilds: FakeGuilds | None = None,
    **settings: Any,
) -> tuple[Summarize, AhaBot]:
    bot = AhaBot(Settings(token="t", **settings))
    bot.guild_settings = cast("Any", guilds if guilds is not None else FakeGuilds())
    cog = Summarize(
        bot,
        cast("Any", model if model is not None else FakeModel()),
        cast("Any", usage if usage is not None else FakeUsage()),
    )
    return cog, bot


def run(cog: Summarize, text: str = "Some long text.", **kwargs: Any) -> Any:
    defaults: dict[str, Any] = {"user_id": 33, "guild_id": 11, "locale": "en-US", "text": text}
    return asyncio.run(cog.run(**{**defaults, **kwargs}))


def sample(bot: AhaBot, name: str, **labels: str) -> float | None:
    return bot.metrics.registry.get_sample_value(name, labels)


# ---------------------------------------------------------------------------- the controls


def test_a_summary_is_returned_as_plain_text_and_its_tokens_are_counted() -> None:
    model = FakeModel(Summary("**Bold** @everyone summary", 120, 30))
    usage = FakeUsage()
    cog, bot = make(model=model, usage=usage, summarize_daily_quota=3)

    outcome = run(cog, "  Hello there.  ")

    assert outcome.ok is True
    assert outcome.text == "\\*\\*Bold\\*\\* @​everyone summary"
    assert model.texts == ["Hello there."]  # trimmed
    assert usage.calls == [("consume", 33, 3)]
    assert sample(bot, "aha_summarize_requests_total", outcome="ok") == 1
    assert sample(bot, "aha_summarize_tokens_total", kind="input") == 120
    assert sample(bot, "aha_summarize_tokens_total", kind="output") == 30


def test_the_answer_is_clipped_to_the_embed_limit() -> None:
    cog, _ = make(model=FakeModel(Summary("x" * 9000, 1, 1)))

    outcome = run(cog)

    assert len(outcome.text) == EMBED_LIMIT
    assert outcome.text.endswith("…")


def test_the_input_is_capped_before_it_reaches_the_model() -> None:
    model = FakeModel()
    cog, _ = make(model=model, summarize_max_chars=100)

    run(cog, "a" * 500)

    assert model.texts == ["a" * 100]


def test_without_an_api_key_the_command_is_unavailable_and_nothing_is_stored() -> None:
    bot = AhaBot(Settings(token="t"))
    usage = FakeUsage()
    cog = Summarize(bot, None, cast("Any", usage))

    outcome = run(cog)

    assert outcome.ok is False
    assert outcome.text == "Summaries are not available: no model API key is configured."
    assert usage.calls == []
    assert sample(bot, "aha_summarize_requests_total", outcome="disabled") == 1


def test_without_a_database_it_is_unavailable_too() -> None:
    bot = AhaBot(Settings(token="t"))
    cog = Summarize(bot, cast("Any", FakeModel()), None)

    assert run(cog).text.startswith("Summaries are not available")


def test_it_only_works_in_servers() -> None:
    model = FakeModel()
    cog, _ = make(model=model)

    outcome = run(cog, guild_id=None)

    assert outcome.text == "Summaries only work in servers."
    assert model.texts == []


def test_a_server_that_has_not_opted_in_sends_nothing_to_the_model() -> None:
    model, usage, guilds = FakeModel(), FakeUsage(), FakeGuilds(enabled=False)
    cog, bot = make(model=model, usage=usage, guilds=guilds)

    outcome = run(cog)

    assert outcome.ok is False
    assert outcome.text == (
        "Summaries are off in this server. A server admin can turn them on with "
        "`/configure summaries on`."
    )
    assert guilds.asked == [11]
    assert (model.texts, usage.calls) == ([], [])  # no model call, and no quota spent
    assert sample(bot, "aha_summarize_requests_total", outcome="disabled") == 1


def test_empty_text_costs_nothing() -> None:
    model, usage = FakeModel(), FakeUsage()
    cog, bot = make(model=model, usage=usage)

    outcome = run(cog, "   \n ")

    assert outcome.text == "There is no text to summarize."
    assert (model.texts, usage.calls) == ([], [])
    assert sample(bot, "aha_summarize_requests_total", outcome="empty") == 1


def test_a_user_over_their_daily_quota_gets_a_friendly_refusal() -> None:
    model = FakeModel()
    cog, bot = make(model=model, usage=FakeUsage(limit_hit=True), summarize_daily_quota=4)

    outcome = run(cog)

    assert outcome.ok is False
    assert outcome.text == "You have used all 4 summaries for today (UTC). Try again tomorrow."
    assert model.texts == []
    assert sample(bot, "aha_summarize_requests_total", outcome="quota") == 1


def test_a_failed_model_call_refunds_the_quota_and_logs_only_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    usage = FakeUsage()
    cog, bot = make(model=FakeModel(SummarizeError("the request timed out")), usage=usage)

    with caplog.at_level(logging.WARNING, logger="aha.cogs.summarize"):
        outcome = run(cog, "private words")

    assert outcome.ok is False
    assert outcome.text == (
        "I couldn't produce a summary right now. It did not count against your daily limit."
    )
    assert usage.calls == [("consume", 33, 10), ("refund", 33, None)]
    assert caplog.messages == ["Summary failed: the request timed out"]
    assert "private words" not in caplog.text
    assert sample(bot, "aha_summarize_requests_total", outcome="failed") == 1


def test_the_outcomes_speak_the_users_language() -> None:
    cog, _ = make(guilds=FakeGuilds(enabled=False))
    assert run(cog, locale="de").text.startswith("Zusammenfassungen sind auf diesem Server aus.")

    cog, _ = make(usage=FakeUsage(limit_hit=True), summarize_daily_quota=2)
    assert run(cog, locale="de").text == (
        "Du hast heute (UTC) alle 2 Zusammenfassungen verbraucht. Versuche es morgen wieder."
    )


# ---------------------------------------------------------------------------- the surfaces


def test_the_slash_command_defers_first_and_replies_privately() -> None:
    cog, _ = make()
    ctx = StubContext()

    asyncio.run(cast("Any", cog.summarize.callback)(cog, as_context(ctx), text="Some text."))

    assert ctx.deferred == [True]
    ((_, kwargs),) = ctx.replies
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == "Summary"
    assert kwargs["embed"].description == "It is short."


def test_a_refusal_is_shown_as_an_error_not_a_summary() -> None:
    cog, _ = make(guilds=FakeGuilds(enabled=False))
    ctx = StubContext()

    asyncio.run(cast("Any", cog.summarize.callback)(cog, as_context(ctx), text="Some text."))

    ((_, kwargs),) = ctx.replies
    assert kwargs["embed"].title is None
    assert "turn them on" in kwargs["embed"].description


def test_the_command_is_guild_only_with_a_cooldown_and_a_category() -> None:
    assert Summarize.summarize.extras["category"] == "Fun"
    cooldown = Summarize.summarize.cooldown
    assert cooldown is not None
    assert (cooldown.rate, cooldown.per) == (1, 20)
    assert any("guild_only" in getattr(c, "__qualname__", "") for c in Summarize.summarize.checks)


def test_the_message_menu_defers_then_follows_up_privately_without_mentions() -> None:
    cog, _ = make()
    interaction = StubInteraction()
    message = cast("discord.Message", Named(content="A message to summarize."))

    asyncio.run(cog.summarize_message(as_interaction(interaction), message))

    assert interaction.deferred == [True]
    ((kind, _, kwargs),) = interaction.sent
    assert kind == "followup"
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].description == "It is short."
    assert kwargs["allowed_mentions"].users is False


def test_the_menu_is_registered_and_removed_with_the_cog() -> None:
    cog, bot = make()

    asyncio.run(cog.cog_load())
    assert bot.tree.get_command("Summarize", type=discord.AppCommandType.message) is not None
    asyncio.run(cog.cog_unload())
    assert bot.tree.get_command("Summarize", type=discord.AppCommandType.message) is None
    assert cog._menu.name == "Summarize"


# ------------------------------------------------------------------------------- setup


def test_setup_builds_the_model_client_only_with_a_key() -> None:
    added: list[Any] = []

    class Bot:
        def __init__(self, key: str | None) -> None:
            self.settings = Settings(token="t", anthropic_api_key=key, anthropic_model="m")
            self.db = None

        async def add_cog(self, cog: Any) -> None:
            added.append(cog)

    asyncio.run(summarize_setup(cast("Any", Bot(None))))
    asyncio.run(summarize_setup(cast("Any", Bot("sk-1"))))

    assert added[0]._summarizer is None
    assert isinstance(added[1]._summarizer, AnthropicSummarizer)
    assert added[1]._summarizer._model == "m"
    assert added[1]._summarizer._api_key == "sk-1"


def test_the_usage_store_is_built_over_the_bots_database() -> None:
    bot = AhaBot(Settings(token="t"))
    bot.db = cast("Any", Named())

    cog = Summarize(bot, None)

    assert cast("Any", cog._usage)._db is bot.db


def test_the_menu_callback_runs_the_same_flow() -> None:
    cog, _ = make()
    interaction = StubInteraction()
    message = cast("discord.Message", Named(content="Text from the menu."))

    asyncio.run(cast("Any", cog._menu.callback)(as_interaction(interaction), message))

    assert interaction.sent[0][2]["embed"].description == "It is short."


def test_each_refusal_is_a_failure_with_its_own_wording_in_both_languages() -> None:
    unavailable = Summarize(AhaBot(Settings(token="t")), None, None)
    cog, _ = make()
    cases: list[tuple[Summarize, dict[str, Any], str, str]] = [
        (
            unavailable,
            {},
            "Summaries are not available: no model API key",
            "Zusammenfassungen sind nicht verfügbar",
        ),
        (
            cog,
            {"guild_id": None},
            "Summaries only work in servers.",
            "Zusammenfassungen funktionieren nur auf Servern.",
        ),
        (
            cog,
            {"text": "  "},
            "There is no text to summarize.",
            "Es gibt keinen Text zum Zusammenfassen.",
        ),
    ]
    for target, kwargs, english, german in cases:
        for locale, expected in (("en-US", english), ("de", german)):
            outcome = run(target, locale=locale, **kwargs)
            assert outcome.ok is False
            assert outcome.text.startswith(expected)


def test_the_failure_message_is_german_too() -> None:
    cog, _ = make(model=FakeModel(SummarizeError("x")))

    outcome = run(cog, locale="de")

    assert outcome.ok is False
    assert outcome.text.startswith("Ich konnte gerade keine Zusammenfassung erstellen.")


def test_the_unavailable_case_is_counted_as_disabled_and_a_guild_only_one_too() -> None:
    bot = AhaBot(Settings(token="t"))
    cog = Summarize(bot, None, None)
    run(cog)
    cog2, bot2 = make()
    run(cog2, guild_id=None)

    assert sample(bot, "aha_summarize_requests_total", outcome="disabled") == 1
    assert sample(bot2, "aha_summarize_requests_total", outcome="disabled") == 1


def test_a_partly_configured_cog_is_unavailable() -> None:
    bot = AhaBot(Settings(token="t"))
    bot.guild_settings = None
    cog = Summarize(bot, cast("Any", FakeModel()), cast("Any", FakeUsage()))
    assert run(cog).ok is False

    bot2 = AhaBot(Settings(token="t"))
    bot2.guild_settings = cast("Any", FakeGuilds())
    cog2 = Summarize(bot2, None, cast("Any", FakeUsage()))
    assert run(cog2).ok is False

    cog3 = Summarize(bot2, cast("Any", FakeModel()), None)
    assert run(cog3).ok is False


def test_the_menu_knows_who_asked_and_their_language_and_shows_a_summary_card() -> None:
    usage = FakeUsage()
    cog, _ = make(usage=usage)
    interaction = StubInteraction(user_id=77)
    interaction.locale = discord.Locale.german
    message = cast("discord.Message", Named(content="Ein Text."))

    asyncio.run(cog.summarize_message(as_interaction(interaction), message))

    assert usage.calls == [("consume", 77, 10)]
    ((_, _, kwargs),) = interaction.sent
    assert kwargs["embed"].title == "Zusammenfassung"
    assert kwargs["embed"].description == "It is short."


def test_the_menu_shows_a_refusal_as_an_error_card_in_the_users_language() -> None:
    cog, _ = make(guilds=FakeGuilds(enabled=False))
    interaction = StubInteraction()
    interaction.locale = discord.Locale.german
    message = cast("discord.Message", Named(content="x"))

    asyncio.run(cog.summarize_message(as_interaction(interaction), message))

    ((_, _, kwargs),) = interaction.sent
    assert kwargs["embed"].title is None
    assert kwargs["embed"].description.startswith("Zusammenfassungen sind auf diesem Server aus.")


def test_a_cog_without_a_database_has_no_usage_store() -> None:
    assert Summarize(AhaBot(Settings(token="t")), None)._usage is None
