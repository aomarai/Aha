"""Tests for :mod:`aha.config`."""

from __future__ import annotations

import pytest

from aha.config import ConfigError, Settings


def test_from_env_requires_token() -> None:
    with pytest.raises(ConfigError, match="DISCORD_BOT_TOKEN"):
        Settings.from_env({})


def test_from_env_rejects_blank_token() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"DISCORD_BOT_TOKEN": "   "})


def test_from_env_applies_defaults() -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret"})

    assert settings.token == "secret"
    assert settings.command_prefix == "!"
    assert settings.log_level == "INFO"
    assert settings.dev_guild_id is None


def test_from_env_reads_and_normalises_overrides() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_BOT_TOKEN": "  secret  ",
            "COMMAND_PREFIX": "$",
            "LOG_LEVEL": "debug",
            "DEV_GUILD_ID": "123456789012345678",
        }
    )

    assert settings.token == "secret"
    assert settings.command_prefix == "$"
    assert settings.log_level == "DEBUG"
    assert settings.dev_guild_id == 123456789012345678


def test_dev_guild_id_rejects_non_integer() -> None:
    with pytest.raises(ConfigError, match="DEV_GUILD_ID"):
        Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "DEV_GUILD_ID": "not-an-int"})


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "LOG_LEVEL": "LOUD"})
