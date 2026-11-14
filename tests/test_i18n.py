"""Tests for the message catalogue, the command translator and the shipped locale files."""

from __future__ import annotations

import asyncio
import logging
import string
from pathlib import Path
from typing import Any, cast

import discord
import pytest
from discord import app_commands

from aha.i18n import (
    DEFAULT_LOCALE,
    DESCRIPTION_LIMIT,
    AhaTranslator,
    Catalogue,
    default_catalogue,
    locale_of,
    t,
)
from tests.helpers import Named, StubContext, StubInteraction, as_interaction


def catalogue() -> Catalogue:
    return Catalogue(
        {
            "en-US": {"hi": "Hello {name}", "only.base": "base", "bad": "{missing}"},
            "de": {"hi": "Hallo {name}", "bad": "{oops}", "broken": "{"},
            "de-AT": {"hi": "Servus {name}"},
        },
        {"de": {"Roll a die.": "Würfeln."}, "de-AT": {"Flip a coin.": "Münze."}},
    )


# --------------------------------------------------------------------------- messages


def test_the_exact_locale_wins_then_the_language_then_the_base() -> None:
    c = catalogue()

    assert c.message("hi", "de-AT", name="Ana") == "Servus Ana"
    assert c.message("hi", "de-CH", name="Ana") == "Hallo Ana"  # falls back to the language
    assert c.message("hi", "fr", name="Ana") == "Hello Ana"  # and finally to the base
    assert c.message("hi", name="Ana") == "Hello Ana"  # the default locale is the base


def test_a_key_missing_from_a_translation_falls_back_to_the_base() -> None:
    assert catalogue().message("only.base", "de") == "base"


def test_an_unknown_key_returns_the_key_and_logs_it(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="aha.i18n"):
        assert catalogue().message("nope", "de") == "nope"

    assert caplog.messages == ["Missing message 'nope'"]


def test_a_translation_that_does_not_format_uses_the_base_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    c = catalogue()

    with caplog.at_level(logging.WARNING, logger="aha.i18n"):
        # German "{oops}" has no value; the base "{missing}" has none either, so raw base text.
        assert c.message("bad", "de", name="x") == "{missing}"
        assert c.message("broken", "de") == "broken"  # a stray "{" in German; no base: the key

    assert "Bad de template for message 'bad'" in caplog.messages


def test_a_bad_base_template_never_raises() -> None:
    assert catalogue().message("bad", "en-US") == "{missing}"


def test_missing_values_in_the_base_template_do_not_raise() -> None:
    assert catalogue().message("hi") == "Hello {name}"


def test_the_language_tag_of_a_regional_locale_is_not_double_counted() -> None:
    assert catalogue()._chain("de") == ["de", "en-US"]
    assert catalogue()._chain("en-US") == ["en-US", "en"]
    assert catalogue()._chain("en-GB") == ["en-GB", "en", "en-US"]


def test_the_shipped_catalogue_lists_its_locales() -> None:
    assert default_catalogue().locales == ["de", "en-US"]


# ------------------------------------------------------------------ command translation


def test_command_text_is_looked_up_by_english_text_with_the_same_fallback() -> None:
    c = catalogue()

    assert c.command_text("Roll a die.", "de") == "Würfeln."
    assert c.command_text("Roll a die.", "de-AT") == "Würfeln."  # the language is the fallback
    assert c.command_text("Flip a coin.", "de-AT") == "Münze."
    assert c.command_text("Flip a coin.", "de") is None
    assert c.command_text("Roll a die.", "en-US") is None  # the base language is the English
    assert c.command_text("Unknown.", "de") is None


def translate(translator: AhaTranslator, text: str, locale: discord.Locale) -> str | None:
    context = cast("Any", Named())
    return asyncio.run(translator.translate(app_commands.locale_str(text), locale, context))


def test_the_translator_returns_the_catalogue_text_or_none() -> None:
    translator = AhaTranslator(catalogue())

    assert translate(translator, "Roll a die.", discord.Locale.german) == "Würfeln."
    assert translate(translator, "Roll a die.", discord.Locale.american_english) is None
    assert translate(translator, "Unknown.", discord.Locale.german) is None


def test_a_translation_over_discords_limit_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    long = Catalogue({}, {"de": {"x": "y" * DESCRIPTION_LIMIT, "z": "y" * (DESCRIPTION_LIMIT + 1)}})
    translator = AhaTranslator(long)

    with caplog.at_level(logging.WARNING, logger="aha.i18n"):
        assert translate(translator, "x", discord.Locale.german) == "y" * DESCRIPTION_LIMIT
        assert translate(translator, "z", discord.Locale.german) is None

    assert caplog.messages == ["Ignoring a translation longer than 100 characters"]


def test_the_translator_defaults_to_the_shipped_catalogue() -> None:
    assert (
        translate(AhaTranslator(), "Roll a die.", discord.Locale.german) == "Würfelt einen Würfel."
    )


def test_discordpy_already_marks_command_text_translatable() -> None:
    """If this ever stops being true the translator would silently see nothing."""
    from aha.commands import hybrid_command

    @hybrid_command(name="demo", description="Roll a die.")
    async def demo(ctx: Any) -> None:  # pragma: no cover - never invoked
        return None

    app_command = cast("Any", demo).app_command
    assert isinstance(app_command._locale_name, app_commands.locale_str)
    assert isinstance(app_command._locale_description, app_commands.locale_str)
    assert app_command._locale_description.message == "Roll a die."


# --------------------------------------------------------------------------- locale_of


def test_the_locale_is_the_users_client_language() -> None:
    ctx = StubContext()
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=discord.Locale.french)

    assert locale_of(ctx) == "de"


def test_the_servers_language_is_the_fallback() -> None:
    ctx = StubContext()
    ctx.interaction = Named(locale=None, guild_locale=discord.Locale.french)

    assert locale_of(ctx) == "fr"


def test_a_message_invocation_or_unknown_locale_gets_the_base_locale() -> None:
    assert locale_of(StubContext()) == DEFAULT_LOCALE
    ctx = StubContext()
    ctx.interaction = Named(locale=None, guild_locale=None)
    assert locale_of(ctx) == DEFAULT_LOCALE


def test_an_interaction_can_be_passed_directly() -> None:
    interaction = StubInteraction()

    real = object.__new__(discord.Interaction)  # an Interaction without a connection state
    real.locale = discord.Locale.japanese
    real.guild_locale = None

    assert locale_of(real) == "ja"
    assert locale_of(as_interaction(interaction)) == DEFAULT_LOCALE  # a stub with no locale sent
    interaction.locale = discord.Locale.german
    assert locale_of(as_interaction(interaction)) == "de"


def test_t_uses_the_shipped_catalogue() -> None:
    assert t("coin.heads") == "Heads"
    assert t("coin.heads", "de") == "Kopf"
    assert t("coin.result", "de", side="Zahl") == "Zahl!"


# ------------------------------------------------------------- the shipped locale files


def placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_every_translation_has_the_same_placeholders_as_the_base() -> None:
    c = default_catalogue()
    base = c._messages[DEFAULT_LOCALE]

    for locale in c.locales:
        for key, template in c._messages[locale].items():
            assert key in base, f"{locale}: {key} has no base message"
            assert placeholders(template) == placeholders(base[key]), f"{locale}: {key}"


def test_every_command_translation_fits_discord_and_names_a_real_command() -> None:
    from aha.bot import COG_MODULES, AhaBot
    from aha.config import Settings

    bot = AhaBot(Settings(token="test-token"))

    async def collect() -> set[str]:
        for module in COG_MODULES:
            await bot.load_extension(module)
        return {c.description for c in bot.tree.walk_commands()}

    descriptions = asyncio.run(collect())
    c = default_catalogue()
    for locale in c.locales:
        for english, translated in c._commands[locale].items():
            assert english in descriptions, f"{locale}: stale entry {english!r}"
            assert 1 <= len(translated) <= DESCRIPTION_LIMIT, f"{locale}: {english!r}"


def test_the_json_files_are_utf8_and_end_with_a_newline() -> None:
    folder = Path(__file__).parent.parent / "src" / "aha" / "locales"
    for path in folder.glob("*.json"):
        assert path.read_text(encoding="utf-8").endswith("}\n"), path.name


def test_a_catalogue_loads_from_any_directory(tmp_path: Path) -> None:
    (tmp_path / "xx.json").write_text(
        '{"messages": {"k": "v"}, "commands": {"a": "b"}}', encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("ignored", encoding="utf-8")
    (tmp_path / "yy.json").write_text("{}", encoding="utf-8")

    loaded = Catalogue.load(tmp_path)

    assert loaded.locales == ["xx", "yy"]
    assert loaded.message("k", "xx") == "v"
    assert loaded.command_text("a", "xx") == "b"
    assert loaded.command_text("a", "yy") is None


def test_the_default_directory_is_the_packaged_catalogue_read_as_utf8() -> None:
    loaded = Catalogue.load()

    assert loaded.locales == ["de", "en-US"]
    assert loaded.command_text("Show what the bot stores and where the policy is.", "de") == (
        "Zeigt, was der Bot speichert und wo die Datenschutzerklärung steht."
    )


class _Entry:
    def __init__(self, name: str, text: str) -> None:
        self.name = name
        self._text = text

    def read_text(self, encoding: str | None = None) -> str:
        assert encoding == "utf-8", f"expected utf-8, got {encoding!r}"
        return self._text


class _Dir:
    def __init__(self, *entries: _Entry) -> None:
        self._entries = entries

    def iterdir(self) -> Any:
        return iter(self._entries)


def test_files_are_read_as_utf8_in_name_order_and_missing_sections_are_empty() -> None:
    folder = _Dir(_Entry("zz.json", "{}"), _Entry("aa.json", '{"messages": {"k": "ä"}}'))

    loaded = Catalogue.load(cast("Any", folder))

    assert loaded.locales == ["aa", "zz"]
    assert loaded.message("k", "aa") == "ä"
    assert loaded.message("k", "zz") == "k"  # zz has no messages, and no base: the key
    assert loaded.command_text("anything", "zz") is None


def test_a_catalogue_without_a_base_locale_still_answers_with_the_key() -> None:
    assert Catalogue({}, {}).message("k") == "k"


def test_a_context_interaction_wins_over_a_locale_on_the_context_itself() -> None:
    ctx = Named(
        interaction=Named(locale=discord.Locale.german, guild_locale=None),
        locale=discord.Locale.french,
    )

    assert locale_of(ctx) == "de"
