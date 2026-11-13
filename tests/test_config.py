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


def test_settings_repr_redacts_the_token() -> None:
    settings = Settings(token="super-secret", log_level="DEBUG", dev_guild_id=42)

    rendered = repr(settings)

    assert "super-secret" not in rendered
    assert "log_level='DEBUG'" in rendered
    assert "dev_guild_id=42" in rendered


def test_log_format_defaults_to_text() -> None:
    assert Settings.from_env({"DISCORD_BOT_TOKEN": "secret"}).log_format == "text"


@pytest.mark.parametrize("raw", ["json", "JSON", " Json "])
def test_log_format_json_is_accepted_case_insensitively(raw: str) -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "LOG_FORMAT": raw})

    assert settings.log_format == "json"


def test_blank_log_format_falls_back_to_text() -> None:
    assert Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "LOG_FORMAT": ""}).log_format == "text"


def test_invalid_log_format_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"^LOG_FORMAT must be 'text' or 'json', got 'yaml'$"):
        Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "LOG_FORMAT": "yaml"})


def test_sync_on_startup_defaults_to_false() -> None:
    assert Settings.from_env({"DISCORD_BOT_TOKEN": "secret"}).sync_on_startup is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "On"])
def test_sync_on_startup_accepts_truthy_values(raw: str) -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "SYNC_ON_STARTUP": raw})

    assert settings.sync_on_startup is True


@pytest.mark.parametrize("raw", ["0", "false", "False", " no ", "OFF", ""])
def test_sync_on_startup_accepts_falsy_and_blank_values(raw: str) -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "SYNC_ON_STARTUP": raw})

    assert settings.sync_on_startup is False


def test_invalid_sync_on_startup_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"^SYNC_ON_STARTUP must be true or false, got 'maybe'$"):
        Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "SYNC_ON_STARTUP": "maybe"})


def test_health_server_defaults_to_loopback_port_8080() -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret"})

    assert (settings.health_bind, settings.health_port) == ("127.0.0.1", 8080)


def test_health_server_address_is_configurable() -> None:
    settings = Settings.from_env(
        {"DISCORD_BOT_TOKEN": "secret", "HEALTH_BIND": " 0.0.0.0 ", "HEALTH_PORT": "9100"}
    )

    assert (settings.health_bind, settings.health_port) == ("0.0.0.0", 9100)


def test_blank_health_settings_fall_back_to_the_defaults() -> None:
    settings = Settings.from_env(
        {"DISCORD_BOT_TOKEN": "secret", "HEALTH_BIND": "", "HEALTH_PORT": " "}
    )

    assert (settings.health_bind, settings.health_port) == ("127.0.0.1", 8080)


@pytest.mark.parametrize("raw", [" ", "\t", "  \n"])
def test_a_whitespace_only_health_bind_falls_back_to_loopback(raw: str) -> None:
    # An empty bind address would make the server listen on every interface.
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "HEALTH_BIND": raw})

    assert settings.health_bind == "127.0.0.1"


def test_health_port_zero_disables_the_server() -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "HEALTH_PORT": "0"})

    assert settings.health_port == 0


@pytest.mark.parametrize("raw", ["-1", "65536", "99999"])
def test_health_port_out_of_range_is_rejected(raw: str) -> None:
    with pytest.raises(ConfigError, match=r"^HEALTH_PORT must be 0 \(disabled\) or 1\.\.65535"):
        Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "HEALTH_PORT": raw})


def test_health_port_must_be_an_integer() -> None:
    with pytest.raises(ConfigError, match="HEALTH_PORT must be an integer"):
        Settings.from_env({"DISCORD_BOT_TOKEN": "secret", "HEALTH_PORT": "http"})


def test_health_port_boundary_values_are_accepted() -> None:
    assert Settings.from_env({"DISCORD_BOT_TOKEN": "s", "HEALTH_PORT": "1"}).health_port == 1
    assert (
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "HEALTH_PORT": "65535"}).health_port == 65535
    )
