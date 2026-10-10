"""Smoke tests for :mod:`aha.bot` (no network/gateway connection)."""

from __future__ import annotations

from aha.bot import COG_MODULES, AhaBot
from aha.config import Settings


def test_bot_constructs_and_stores_settings() -> None:
    settings = Settings(token="test-token", command_prefix="$")
    bot = AhaBot(settings)

    assert bot.settings is settings
    assert bot.command_prefix is not None


def test_cog_modules_are_configured_as_extensions() -> None:
    # Extensions are dotted module paths that must live under aha.cogs.
    assert COG_MODULES
    assert all(module.startswith("aha.cogs.") for module in COG_MODULES)
