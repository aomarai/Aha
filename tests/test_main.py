"""Tests for the entry point (:mod:`aha.__main__`)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from aha import __main__ as aha_main
from aha.bot import AhaBot

#: Environment variables :meth:`Settings.from_env` reads, besides the token.
_SETTINGS_ENV_VARS = ("LOG_LEVEL", "DEV_GUILD_ID")


def _isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run from an empty directory with no ambient settings inherited.

    ``main`` loads a ``.env`` from the current directory and then reads the
    process environment, so a stray ``.env`` or an inherited ``LOG_LEVEL`` /
    ``DEV_GUILD_ID`` would change the outcome of these tests.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_main_returns_one_without_a_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_environment(monkeypatch, tmp_path)

    assert aha_main.main() == 1


def test_main_runs_the_bot_with_valid_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")

    started: dict[str, Any] = {}

    def fake_run(self: AhaBot, token: str, **kwargs: Any) -> None:
        started["token"] = token

    monkeypatch.setattr(AhaBot, "run", fake_run)

    assert aha_main.main() == 0
    assert started["token"] == "dummy-token"


def test_main_loads_dotenv_from_the_current_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(aha_main, "load_dotenv", lambda **kwargs: seen.update(kwargs))

    assert aha_main.main() == 1  # no token: load_dotenv stubbed out
    assert seen == {"dotenv_path": tmp_path / ".env"}


def test_main_reports_configuration_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    basic_config_kwargs: dict[str, Any] = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: basic_config_kwargs.update(kw))

    assert aha_main.main() == 1

    assert basic_config_kwargs == {"level": logging.ERROR}
    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.msg == "%s"
    assert "DISCORD_BOT_TOKEN is not set" in record.getMessage()


def test_main_configures_logging_and_disables_discord_log_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    configured: list[str] = []
    run_kwargs: dict[str, Any] = {}

    def fake_run(self: AhaBot, token: str, **kwargs: Any) -> None:
        run_kwargs.update(kwargs)

    monkeypatch.setattr(aha_main, "configure_logging", configured.append)
    monkeypatch.setattr(AhaBot, "run", fake_run)

    assert aha_main.main() == 0
    assert configured == ["DEBUG"]
    assert run_kwargs == {"log_handler": None}
