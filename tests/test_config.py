"""Tests for :mod:`aha.config`."""

from __future__ import annotations

from pathlib import Path

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


def _write(tmp_path: Path, content: str | bytes, name: str = "token") -> str:
    path = tmp_path / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return str(path)


def test_token_can_be_read_from_a_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "file-secret\n")  # Kubernetes and editors add a trailing newline

    settings = Settings.from_env({"DISCORD_BOT_TOKEN_FILE": path})

    assert settings.token == "file-secret"


def test_token_file_path_is_trimmed(tmp_path: Path) -> None:
    path = _write(tmp_path, "file-secret")

    settings = Settings.from_env({"DISCORD_BOT_TOKEN_FILE": f"  {path}  "})

    assert settings.token == "file-secret"


def test_a_blank_token_file_variable_falls_back_to_the_environment_token() -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "inline", "DISCORD_BOT_TOKEN_FILE": "  "})

    assert settings.token == "inline"


def test_setting_both_the_token_and_a_token_file_is_an_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "file-secret")

    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "inline", "DISCORD_BOT_TOKEN_FILE": path})

    assert (
        str(excinfo.value)
        == "Set only one of DISCORD_BOT_TOKEN and DISCORD_BOT_TOKEN_FILE, not both."
    )


def test_a_missing_token_file_is_reported_with_its_path(tmp_path: Path) -> None:
    missing = str(tmp_path / "nope")

    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN_FILE": missing})

    assert str(excinfo.value) == (
        f"Cannot read DISCORD_BOT_TOKEN_FILE ({missing}): No such file or directory."
    )


def test_a_token_file_that_is_a_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"^Cannot read DISCORD_BOT_TOKEN_FILE \(.*\): "):
        Settings.from_env({"DISCORD_BOT_TOKEN_FILE": str(tmp_path)})


def test_a_binary_token_file_is_reported_without_echoing_its_content(tmp_path: Path) -> None:
    path = _write(tmp_path, b"\xff\xfe\x00secret")

    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN_FILE": path})

    assert str(excinfo.value) == f"Cannot read DISCORD_BOT_TOKEN_FILE ({path}): not valid UTF-8."
    assert "secret" not in str(excinfo.value)


@pytest.mark.parametrize("content", ["", "  \n\t\n"])
def test_an_empty_token_file_is_rejected(tmp_path: Path, content: str) -> None:
    path = _write(tmp_path, content)

    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN_FILE": path})

    assert str(excinfo.value) == f"DISCORD_BOT_TOKEN_FILE ({path}) is empty."


def test_a_token_read_from_a_file_is_still_redacted_in_the_repr(tmp_path: Path) -> None:
    path = _write(tmp_path, "file-secret")

    settings = Settings.from_env({"DISCORD_BOT_TOKEN_FILE": path})

    assert "file-secret" not in repr(settings)


def test_presence_defaults_to_listening_to_help_and_online() -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "secret"})

    assert (settings.activity, settings.activity_type, settings.status) == (
        "/help",
        "listening",
        "online",
    )


def test_presence_is_configurable() -> None:
    settings = Settings.from_env(
        {
            "DISCORD_BOT_TOKEN": "secret",
            "BOT_ACTIVITY": "  with dice  ",
            "BOT_ACTIVITY_TYPE": " Playing ",
            "BOT_STATUS": "DND",
        }
    )

    assert (settings.activity, settings.activity_type, settings.status) == (
        "with dice",
        "playing",
        "dnd",
    )


@pytest.mark.parametrize("raw", ["off", "OFF", " Off "])
def test_activity_off_shows_no_activity(raw: str) -> None:
    assert Settings.from_env({"DISCORD_BOT_TOKEN": "s", "BOT_ACTIVITY": raw}).activity is None


def test_a_blank_presence_setting_falls_back_to_the_defaults() -> None:
    settings = Settings.from_env(
        {"DISCORD_BOT_TOKEN": "s", "BOT_ACTIVITY": " ", "BOT_ACTIVITY_TYPE": "", "BOT_STATUS": " "}
    )

    assert (settings.activity, settings.activity_type, settings.status) == (
        "/help",
        "listening",
        "online",
    )


@pytest.mark.parametrize("status", ["online", "idle", "dnd", "invisible"])
def test_every_discord_status_is_accepted(status: str) -> None:
    assert Settings.from_env({"DISCORD_BOT_TOKEN": "s", "BOT_STATUS": status}).status == status


@pytest.mark.parametrize("kind", ["playing", "listening", "watching", "competing"])
def test_every_supported_activity_type_is_accepted(kind: str) -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "s", "BOT_ACTIVITY_TYPE": kind})

    assert settings.activity_type == kind


def test_an_unknown_status_is_rejected_listing_the_choices() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "BOT_STATUS": "away"})

    assert (
        str(excinfo.value) == "BOT_STATUS must be one of dnd, idle, invisible, online, got 'away'"
    )


def test_an_unknown_activity_type_is_rejected_listing_the_choices() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "BOT_ACTIVITY_TYPE": "streaming"})

    assert str(excinfo.value) == (
        "BOT_ACTIVITY_TYPE must be one of competing, listening, playing, watching, got 'streaming'"
    )


def test_an_activity_at_discords_length_limit_is_accepted_and_one_over_is_not() -> None:
    assert Settings.from_env({"DISCORD_BOT_TOKEN": "s", "BOT_ACTIVITY": "x" * 128}).activity == (
        "x" * 128
    )
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "BOT_ACTIVITY": "x" * 129})

    assert str(excinfo.value) == "BOT_ACTIVITY must be at most 128 characters, got 129"


# ------------------------------------------------------------------------------- database


def test_the_database_is_optional_with_sensible_defaults() -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "s"})

    assert settings.database_url is None
    assert (settings.db_pool_max, settings.db_statement_timeout) == (5, 2.0)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_a_postgres_url_is_accepted_from_the_environment(scheme: str) -> None:
    settings = Settings.from_env(
        {"DISCORD_BOT_TOKEN": "s", "DATABASE_URL": f" {scheme}://u:pw@db:5432/aha "}
    )

    assert settings.database_url == f"{scheme}://u:pw@db:5432/aha"


def test_a_database_url_can_come_from_a_mounted_file(tmp_path: Path) -> None:
    path = _write(tmp_path, "postgresql://u:pw@db/aha\n")

    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DATABASE_URL_FILE": path})

    assert settings.database_url == "postgresql://u:pw@db/aha"


def test_setting_the_database_url_both_ways_is_an_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "postgresql://db/aha")

    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env(
            {
                "DISCORD_BOT_TOKEN": "s",
                "DATABASE_URL": "postgresql://x/y",
                "DATABASE_URL_FILE": path,
            }
        )

    assert str(excinfo.value) == "Set only one of DATABASE_URL and DATABASE_URL_FILE, not both."


def test_an_unreadable_database_url_file_is_reported_by_path(tmp_path: Path) -> None:
    missing = str(tmp_path / "nope")

    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DATABASE_URL_FILE": missing})

    assert str(excinfo.value) == (
        f"Cannot read DATABASE_URL_FILE ({missing}): No such file or directory."
    )


@pytest.mark.parametrize("url", ["mysql://u:secret@h/db", "http://x", "u:secret@h/db", "secret"])
def test_a_non_postgres_url_is_rejected_without_echoing_it(url: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DATABASE_URL": url})

    assert str(excinfo.value) == "DATABASE_URL must start with postgres:// or postgresql://."
    assert "secret" not in str(excinfo.value)


def test_the_database_url_is_redacted_in_the_settings_repr() -> None:
    settings = Settings.from_env(
        {"DISCORD_BOT_TOKEN": "s", "DATABASE_URL": "postgresql://u:hunter2@db/aha"}
    )

    assert "hunter2" not in repr(settings)


def test_the_pool_size_and_statement_timeout_are_configurable() -> None:
    settings = Settings.from_env(
        {"DISCORD_BOT_TOKEN": "s", "DB_POOL_MAX": "12", "DB_STATEMENT_TIMEOUT": "0.5"}
    )

    assert (settings.db_pool_max, settings.db_statement_timeout) == (12, 0.5)


def test_blank_database_tuning_values_use_the_defaults() -> None:
    settings = Settings.from_env(
        {"DISCORD_BOT_TOKEN": "s", "DB_POOL_MAX": " ", "DB_STATEMENT_TIMEOUT": ""}
    )

    assert (settings.db_pool_max, settings.db_statement_timeout) == (5, 2.0)


@pytest.mark.parametrize("raw", ["0", "51", "-1"])
def test_a_pool_size_out_of_range_is_rejected(raw: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DB_POOL_MAX": raw})

    assert str(excinfo.value) == f"DB_POOL_MAX must be 1..50, got {raw}"


def test_the_pool_size_boundaries_are_accepted() -> None:
    assert Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DB_POOL_MAX": "1"}).db_pool_max == 1
    assert Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DB_POOL_MAX": "50"}).db_pool_max == 50


@pytest.mark.parametrize("raw", ["0", "-1", "60.5", "inf", "nan"])
def test_a_statement_timeout_out_of_range_is_rejected(raw: str) -> None:
    with pytest.raises(
        ConfigError, match=r"^DB_STATEMENT_TIMEOUT must be greater than 0 and at most 60"
    ):
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DB_STATEMENT_TIMEOUT": raw})


def test_a_statement_timeout_that_is_not_a_number_is_rejected() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DB_STATEMENT_TIMEOUT": "soon"})

    assert str(excinfo.value) == "DB_STATEMENT_TIMEOUT must be a number of seconds, got 'soon'"


def test_the_statement_timeout_boundaries_are_accepted() -> None:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DB_STATEMENT_TIMEOUT": "60"})
    tiny = Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DB_STATEMENT_TIMEOUT": "0.001"})

    assert (settings.db_statement_timeout, tiny.db_statement_timeout) == (60.0, 0.001)


def test_a_pool_size_that_is_not_a_number_names_the_variable() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_env({"DISCORD_BOT_TOKEN": "s", "DB_POOL_MAX": "lots"})

    assert str(excinfo.value) == "DB_POOL_MAX must be an integer, got 'lots'"


# ------------------------------------------------------------------ shard assignment (#93)


def _shards(**env: str) -> tuple[int | None, tuple[int, ...] | None]:
    settings = Settings.from_env({"DISCORD_BOT_TOKEN": "t", **env})
    return settings.shard_count, settings.shard_ids


def _shard_error(**env: str) -> str:
    with pytest.raises(ConfigError) as caught:
        _shards(**env)
    return str(caught.value)


def test_without_shard_settings_discord_chooses() -> None:
    assert _shards() == (None, None)


def test_a_count_alone_is_valid_and_keeps_every_shard_in_this_process() -> None:
    assert _shards(SHARD_COUNT="4") == (4, None)


def test_explicit_ids_are_parsed_with_whitespace_tolerated() -> None:
    assert _shards(SHARD_COUNT="4", SHARD_IDS="0, 1,2") == (4, (0, 1, 2))
    assert _shards(SHARD_COUNT="1", SHARD_IDS="0") == (1, (0,))


def test_the_last_shard_id_is_in_range_and_the_count_itself_is_not() -> None:
    assert _shards(SHARD_COUNT="4", SHARD_IDS="3") == (4, (3,))
    assert _shard_error(SHARD_COUNT="4", SHARD_IDS="3,4") == (
        "SHARD_IDS [4] are outside 0..3 (SHARD_COUNT)"
    )


def test_a_negative_shard_id_is_rejected() -> None:
    assert _shard_error(SHARD_COUNT="4", SHARD_IDS="-1") == (
        "SHARD_IDS [-1] are outside 0..3 (SHARD_COUNT)"
    )


def test_duplicate_ids_are_rejected() -> None:
    assert _shard_error(SHARD_COUNT="4", SHARD_IDS="0,1,1") == (
        "SHARD_IDS must not repeat a shard, got '0,1,1'"
    )


@pytest.mark.parametrize("raw", ["a", "0,,1", "0;1", "1.5"])
def test_ids_that_are_not_integers_are_rejected(raw: str) -> None:
    assert _shard_error(SHARD_COUNT="4", SHARD_IDS=raw) == (
        f"SHARD_IDS must be comma-separated integers, got {raw!r}"
    )


def test_ids_without_a_count_are_rejected_because_discordpy_requires_it() -> None:
    assert _shard_error(SHARD_IDS="0") == "SHARD_COUNT is required when SHARD_IDS is set"


@pytest.mark.parametrize("raw", ["0", "-1", "10001", "x"])
def test_a_bad_shard_count_is_rejected(raw: str) -> None:
    message = _shard_error(SHARD_COUNT=raw)
    assert message.startswith("SHARD_COUNT must be")


def test_the_shard_count_boundaries_are_accepted() -> None:
    assert _shards(SHARD_COUNT="1") == (1, None)
    assert _shards(SHARD_COUNT="10000") == (10000, None)


def test_a_pod_ordinal_derives_its_block_of_shards() -> None:
    assert _shards(SHARD_COUNT="8", SHARDS_PER_POD="2", POD_INDEX="0") == (8, (0, 1))
    assert _shards(SHARD_COUNT="8", SHARDS_PER_POD="2", POD_INDEX="3") == (8, (6, 7))


def test_the_last_pod_gets_the_remainder() -> None:
    assert _shards(SHARD_COUNT="5", SHARDS_PER_POD="2", POD_INDEX="2") == (5, (4,))


def test_a_pod_beyond_the_shards_is_rejected() -> None:
    assert _shard_error(SHARD_COUNT="5", SHARDS_PER_POD="2", POD_INDEX="3") == (
        "POD_INDEX 3 owns no shards: SHARD_COUNT 5 with 2 per pod covers pods 0..2"
    )


def test_a_pod_ordinal_needs_the_count() -> None:
    assert _shard_error(SHARDS_PER_POD="2", POD_INDEX="0") == (
        "SHARD_COUNT is required when SHARDS_PER_POD is set"
    )


def test_shards_per_pod_needs_a_pod_index() -> None:
    expected = "SHARDS_PER_POD needs POD_INDEX (the pod ordinal, 0 or more)"
    assert _shard_error(SHARD_COUNT="4", SHARDS_PER_POD="2") == expected
    assert _shard_error(SHARD_COUNT="4", SHARDS_PER_POD="2", POD_INDEX="-1") == expected


def test_a_pod_index_without_shards_per_pod_is_rejected() -> None:
    assert _shard_error(SHARD_COUNT="4", POD_INDEX="1") == "POD_INDEX needs SHARDS_PER_POD"


@pytest.mark.parametrize("raw", ["0", "-2"])
def test_shards_per_pod_must_be_positive(raw: str) -> None:
    assert _shard_error(SHARD_COUNT="4", SHARDS_PER_POD=raw, POD_INDEX="0") == (
        f"SHARDS_PER_POD must be at least 1, got {raw}"
    )


def test_explicit_ids_and_a_pod_ordinal_are_mutually_exclusive() -> None:
    assert _shard_error(SHARD_COUNT="4", SHARD_IDS="0", SHARDS_PER_POD="2", POD_INDEX="0") == (
        "Set either SHARD_IDS or SHARDS_PER_POD, not both"
    )


def test_a_blank_shard_ids_value_counts_as_unset() -> None:
    assert _shards(SHARD_COUNT="4", SHARD_IDS="  ") == (4, None)


def test_a_single_shard_per_pod_is_valid() -> None:
    assert _shards(SHARD_COUNT="3", SHARDS_PER_POD="1", POD_INDEX="2") == (3, (2,))


def test_non_integer_pod_settings_name_the_variable() -> None:
    assert _shard_error(SHARD_COUNT="4", SHARDS_PER_POD="two", POD_INDEX="0") == (
        "SHARDS_PER_POD must be an integer, got 'two'"
    )
    assert _shard_error(SHARD_COUNT="4", SHARDS_PER_POD="2", POD_INDEX="first") == (
        "POD_INDEX must be an integer, got 'first'"
    )


# ------------------------------------------------- session-start floor (#94)


def _floor(raw: str | None) -> float:
    env = {"DISCORD_BOT_TOKEN": "t"}
    if raw is not None:
        env["SESSION_START_FLOOR"] = raw
    return Settings.from_env(env).session_start_floor


def test_the_session_floor_defaults_to_ten_percent() -> None:
    assert _floor(None) == 0.1
    assert _floor("  ") == 0.1


def test_the_session_floor_is_configurable_including_zero() -> None:
    assert _floor("0.25") == 0.25
    assert _floor("0") == 0.0
    assert _floor("0.999") == 0.999


@pytest.mark.parametrize("raw", ["1", "1.5", "-0.1", "nan"])
def test_a_floor_outside_zero_to_below_one_is_rejected(raw: str) -> None:
    with pytest.raises(ConfigError) as caught:
        _floor(raw)

    assert str(caught.value) == f"SESSION_START_FLOOR must be at least 0 and below 1, got {raw!r}"


def test_a_floor_that_is_not_a_number_is_rejected() -> None:
    with pytest.raises(ConfigError) as caught:
        _floor("ten")

    assert str(caught.value) == "SESSION_START_FLOOR must be a number, got 'ten'"


# --------------------------------------------------------- /summarize settings (#110)


def _env(**extra: str) -> Settings:
    return Settings.from_env({"DISCORD_BOT_TOKEN": "t", **extra})


def test_summarize_is_off_without_a_key_and_has_conservative_defaults() -> None:
    settings = _env()

    assert settings.anthropic_api_key is None
    assert settings.anthropic_model == "claude-haiku-5-5"
    assert settings.summarize_daily_quota == 10
    assert settings.summarize_max_chars == 4000


def test_the_api_key_comes_from_the_environment_or_a_file_but_never_both(
    tmp_path: Path,
) -> None:
    assert _env(ANTHROPIC_API_KEY=" sk-1 ").anthropic_api_key == "sk-1"
    secret = tmp_path / "key"
    secret.write_text("sk-file\n", encoding="utf-8")
    assert _env(ANTHROPIC_API_KEY_FILE=str(secret)).anthropic_api_key == "sk-file"
    with pytest.raises(ConfigError) as caught:
        _env(ANTHROPIC_API_KEY="a", ANTHROPIC_API_KEY_FILE=str(secret))
    assert (
        str(caught.value)
        == "Set only one of ANTHROPIC_API_KEY and ANTHROPIC_API_KEY_FILE, not both."
    )


def test_the_key_is_not_shown_in_the_settings_repr() -> None:
    assert "sk-secret" not in repr(_env(ANTHROPIC_API_KEY="sk-secret"))


def test_the_model_and_limits_are_configurable() -> None:
    settings = _env(
        ANTHROPIC_MODEL=" claude-sonnet-5-5 ", SUMMARIZE_DAILY_QUOTA="3", SUMMARIZE_MAX_CHARS="500"
    )

    assert (settings.anthropic_model, settings.summarize_daily_quota) == ("claude-sonnet-5-5", 3)
    assert settings.summarize_max_chars == 500


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("SUMMARIZE_DAILY_QUOTA", "0", "SUMMARIZE_DAILY_QUOTA must be 1..1000, got 0"),
        ("SUMMARIZE_DAILY_QUOTA", "1001", "SUMMARIZE_DAILY_QUOTA must be 1..1000, got 1001"),
        ("SUMMARIZE_MAX_CHARS", "99", "SUMMARIZE_MAX_CHARS must be 100..20000, got 99"),
        ("SUMMARIZE_MAX_CHARS", "20001", "SUMMARIZE_MAX_CHARS must be 100..20000, got 20001"),
    ],
)
def test_out_of_range_limits_are_rejected(name: str, raw: str, message: str) -> None:
    with pytest.raises(ConfigError) as caught:
        _env(**{name: raw})

    assert str(caught.value) == message


def test_the_limit_boundaries_are_accepted() -> None:
    settings = _env(SUMMARIZE_DAILY_QUOTA="1000", SUMMARIZE_MAX_CHARS="100")
    assert (settings.summarize_daily_quota, settings.summarize_max_chars) == (1000, 100)
    settings = _env(SUMMARIZE_DAILY_QUOTA="1", SUMMARIZE_MAX_CHARS="20000")
    assert (settings.summarize_daily_quota, settings.summarize_max_chars) == (1, 20000)


def test_a_non_numeric_limit_names_the_variable() -> None:
    with pytest.raises(ConfigError) as caught:
        _env(SUMMARIZE_DAILY_QUOTA="lots")

    assert str(caught.value) == "SUMMARIZE_DAILY_QUOTA must be an integer, got 'lots'"


# ------------------------------------------------------------ MENTION_COMMANDS (#101)


def test_mention_commands_default_on_and_can_be_turned_off() -> None:
    assert _env().mention_commands is True
    assert _env(MENTION_COMMANDS="false").mention_commands is False
    assert _env(MENTION_COMMANDS="0").mention_commands is False
    assert _env(MENTION_COMMANDS="yes").mention_commands is True


def test_a_malformed_mention_commands_value_is_rejected() -> None:
    with pytest.raises(ConfigError) as caught:
        _env(MENTION_COMMANDS="sometimes")

    assert str(caught.value) == "MENTION_COMMANDS must be true or false, got 'sometimes'"
