"""A small message catalogue and command-metadata translator (#18).

Two things are translated:

* **Replies**: ``t("ping.latency", locale, latency=...)`` looks the key up in the user's locale,
  then the language alone (``de-AT`` falls back to ``de``), then the base ``en-US``. It **never
  raises**: an unknown key returns the key itself and a template that does not format returns the
  base text, so a bad translation cannot take a command down.
* **Command names and descriptions**: :class:`AhaTranslator` gives Discord the localized text for
  each command, keyed by its English description. discord.py wraps every command name and
  description in ``locale_str`` itself, so no command needs to opt in.

The locale of a reply is the invoking *user's* Discord client language (``interaction.locale``), as
Discord intends. A per-server override can reuse the reserved ``guild_settings.locale`` column
later. Catalogue files live in :mod:`aha.locales`; see ``docs/i18n.md`` for adding a language.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from functools import lru_cache
from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Any

import discord
from discord import app_commands

__all__ = ["DEFAULT_LOCALE", "AhaTranslator", "Catalogue", "default_catalogue", "locale_of", "t"]

log = logging.getLogger(__name__)

#: The base locale: always complete, and where every lookup ends.
DEFAULT_LOCALE = "en-US"

#: Discord limits a command description to this many characters.
DESCRIPTION_LIMIT = 100


class Catalogue:
    """Messages (by key) and command text (by English description), per locale."""

    def __init__(
        self,
        messages: Mapping[str, Mapping[str, str]],
        commands: Mapping[str, Mapping[str, str]],
    ) -> None:
        self._messages = messages
        self._commands = commands

    @classmethod
    def load(cls, directory: Traversable | None = None) -> Catalogue:
        """Read every ``<locale>.json`` from ``directory`` (default: the package's own)."""
        folder = directory if directory is not None else files("aha.locales")
        messages: dict[str, Mapping[str, str]] = {}
        commands: dict[str, Mapping[str, str]] = {}
        for entry in sorted(folder.iterdir(), key=lambda item: item.name):
            if not entry.name.endswith(".json"):
                continue
            data: dict[str, Any] = json.loads(entry.read_text(encoding="utf-8"))
            locale = entry.name.removesuffix(".json")
            messages[locale] = data.get("messages", {})
            commands[locale] = data.get("commands", {})
        return cls(messages, commands)

    @property
    def locales(self) -> list[str]:
        """The locales that have a catalogue file, sorted."""
        return sorted(self._messages)

    def _chain(self, locale: str) -> list[str]:
        """``de-AT`` -> ``de-AT``, ``de``, then the base locale."""
        chain = [locale]
        language = locale.split("-")[0]
        if language != locale:
            chain.append(language)
        if DEFAULT_LOCALE not in chain:
            chain.append(DEFAULT_LOCALE)
        return chain

    def message(self, key: str, locale: str = DEFAULT_LOCALE, **values: object) -> str:
        """Return ``key`` in ``locale``, formatted with ``values``; never raises."""
        chain = self._chain(locale)
        for candidate in chain:
            template = self._messages.get(candidate, {}).get(key)
            if template is None:
                continue
            try:
                return template.format_map(values)
            except KeyError, IndexError, ValueError:
                log.warning("Bad %s template for message %r", candidate, key)
                break
        base = self._messages.get(DEFAULT_LOCALE, {}).get(key)
        if base is None:
            log.warning("Missing message %r", key)
            return key
        try:
            return base.format_map(values)
        except KeyError, IndexError, ValueError:
            return base

    def command_text(self, english: str, locale: str) -> str | None:
        """The translation of a command name or description, or ``None`` for the base language."""
        for candidate in self._chain(locale)[:-1]:  # the base locale is the English itself
            translated = self._commands.get(candidate, {}).get(english)
            if translated is not None:
                return translated
        return None


@lru_cache(maxsize=1)
def default_catalogue() -> Catalogue:
    """The catalogue shipped in :mod:`aha.locales`, loaded once."""
    return Catalogue.load()


def t(key: str, locale: str = DEFAULT_LOCALE, **values: object) -> str:
    """Translate ``key`` into ``locale`` using the shipped catalogue."""
    return default_catalogue().message(key, locale, **values)


def locale_of(source: Any) -> str:
    """The locale to reply in for a command context or an interaction.

    The invoking user's client language, else the server's preferred language, else the base locale.
    A message (prefix) invocation has neither, so it gets the base locale.
    """
    interaction = getattr(source, "interaction", None)
    if interaction is None and (
        isinstance(source, discord.Interaction) or hasattr(source, "locale")
    ):
        interaction = source
    if interaction is None:
        return DEFAULT_LOCALE
    for name in ("locale", "guild_locale"):
        candidate = getattr(interaction, name, None)
        if candidate is not None:
            return str(candidate.value)
    return DEFAULT_LOCALE


class AhaTranslator(app_commands.Translator):
    """Supplies localized command names and descriptions from the catalogue."""

    def __init__(self, catalogue: Catalogue | None = None) -> None:
        super().__init__()
        self._catalogue = catalogue if catalogue is not None else default_catalogue()

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContextTypes,
    ) -> str | None:
        """Return the translation of ``string`` for ``locale``, or ``None`` to keep the English."""
        text = self._catalogue.command_text(string.message, str(locale.value))
        if text is not None and len(text) > DESCRIPTION_LIMIT:
            log.warning("Ignoring a translation longer than %s characters", DESCRIPTION_LIMIT)
            return None
        return text
