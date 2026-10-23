"""Tests for :mod:`aha.config`."""

from __future__ import annotations

import pytest

from aha.config import ConfigError, Settings


def test_from_env_requires_token() -> None:
    with pytest.raises(ConfigError, match="DISCORD_BOT_TOKEN") as excinfo:
        Settings.from_env({})

    assert str(excinfo.value) == (
        "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and add "
        "your bot token, then retry."
    )


def test_from_env_rejects_blank_token() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({"DISCORD_BOT_TOKEN": "   "})


def test_from_env_applies_defaults() -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret"})

    assert settings.token == "secret"
    assert settings.log_level == "INFO"
    assert settings.dev_guild_id is None


def test_from_env_reads_and_normalises_overrides() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_BOT_TOKEN": "  secret  ",
            "LOG_LEVEL": "debug",
            "DEV_GUILD_ID": "123456789012345678",
        }
    )

    assert settings.token == "secret"
    assert settings.log_level == "DEBUG"
    assert settings.dev_guild_id == 123456789012345678


def test_dev_guild_id_rejects_non_integer() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "DEV_GUILD_ID": "not-an-int"})

    assert str(excinfo.value) == "DEV_GUILD_ID must be an integer, got 'not-an-int'"


@pytest.mark.parametrize("bad_value", ["0", "-1", "999999999999999999999999"])
def test_dev_guild_id_rejects_out_of_range(bad_value: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "DEV_GUILD_ID": bad_value})

    assert str(excinfo.value).startswith(
        "DEV_GUILD_ID must be a positive Discord snowflake (1..9223372036854775807), got "
    )


@pytest.mark.parametrize("edge_value", [1, 9223372036854775807])
def test_dev_guild_id_accepts_range_boundaries(edge_value: int) -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "DEV_GUILD_ID": str(edge_value)})

    assert settings.dev_guild_id == edge_value


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "LOG_LEVEL": "LOUD"})
