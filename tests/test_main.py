"""Tests for the entry point (:mod:`aha.__main__`)."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any, cast

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

    async def fake_serve(bot: AhaBot, token: str) -> None:
        started["bot"] = bot
        started["token"] = token

    monkeypatch.setattr(aha_main, "_serve", fake_serve)

    assert aha_main.main() == 0
    assert started["token"] == "dummy-token"
    assert isinstance(started["bot"], AhaBot)
    assert started["bot"].settings.token == "dummy-token"


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


def test_main_configures_logging_before_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    events: list[str] = []

    async def fake_serve(bot: AhaBot, token: str) -> None:
        events.append("serve")

    monkeypatch.setattr(aha_main, "configure_logging", lambda level: events.append(level))
    monkeypatch.setattr(aha_main, "_serve", fake_serve)

    assert aha_main.main() == 0
    assert events == ["DEBUG", "serve"]


def test_main_exits_cleanly_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")

    async def fake_serve(bot: AhaBot, token: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(aha_main, "configure_logging", lambda level: None)
    monkeypatch.setattr(aha_main, "_serve", fake_serve)

    with caplog.at_level(logging.INFO, logger="aha.__main__"):
        assert aha_main.main() == 0
    assert "Interrupted; bot closed" in [r.getMessage() for r in caplog.records]


class _FakeBot:
    """Stands in for AhaBot: ``start`` blocks until closed, like the gateway."""

    def __init__(self, *, fail_start: BaseException | None = None) -> None:
        self.events: list[str] = []
        self._closed = asyncio.Event()
        self._fail_start = fail_start

    async def __aenter__(self) -> _FakeBot:
        self.events.append("enter")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.events.append("exit")

    async def start(self, token: str) -> None:
        self.events.append(f"start:{token}")
        if self._fail_start is not None:
            raise self._fail_start
        await self._closed.wait()

    async def close(self) -> None:
        self.events.append("close")
        self._closed.set()


def _capture_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[signal.Signals, Any]:
    handlers: dict[signal.Signals, Any] = {}

    def add_signal_handler(self: Any, sig: signal.Signals, callback: Any) -> None:
        handlers[sig] = callback

    monkeypatch.setattr(asyncio.AbstractEventLoop, "add_signal_handler", add_signal_handler)
    return handlers


def test_serve_closes_the_bot_when_sigterm_arrives(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    handlers = _capture_signal_handlers(monkeypatch)
    bot = _FakeBot()

    async def scenario() -> None:
        serving = asyncio.create_task(aha_main._serve(cast("AhaBot", bot), "tok"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
        handlers[signal.SIGTERM]()  # what the event loop does on SIGTERM
        await serving

    with caplog.at_level(logging.INFO, logger="aha.__main__"):
        asyncio.run(scenario())

    assert bot.events == ["enter", "start:tok", "close", "exit"]
    assert "Shutdown signal received; closing bot" in [r.getMessage() for r in caplog.records]


def test_serve_propagates_start_failures_with_a_shutdown_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _capture_signal_handlers(monkeypatch)
    bot = _FakeBot(fail_start=RuntimeError("login failed"))

    with (
        caplog.at_level(logging.INFO, logger="aha.__main__"),
        pytest.raises(RuntimeError, match="login failed"),
    ):
        asyncio.run(aha_main._serve(cast("AhaBot", bot), "tok"))

    assert bot.events == ["enter", "start:tok", "close", "exit"]
    assert "Unexpected error during bot startup: login failed" in [
        record.getMessage() for record in caplog.records
    ]


def test_serve_tolerates_loops_without_signal_support(monkeypatch: pytest.MonkeyPatch) -> None:
    def unsupported(self: Any, sig: signal.Signals, callback: Any) -> None:
        raise NotImplementedError

    monkeypatch.setattr(asyncio.AbstractEventLoop, "add_signal_handler", unsupported)
    bot = _FakeBot(fail_start=RuntimeError("stop here"))

    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(aha_main._serve(cast("AhaBot", bot), "tok"))
