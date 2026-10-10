"""Tests for the entry point (:mod:`aha.__main__`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aha import __main__ as aha_main
from aha.bot import AhaBot


def test_main_returns_one_without_a_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Run from an empty directory so no stray .env can supply a token.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    assert aha_main.main() == 1


def test_main_runs_the_bot_with_valid_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")

    started: dict[str, Any] = {}

    def fake_run(self: AhaBot, token: str, **kwargs: Any) -> None:
        started["token"] = token

    monkeypatch.setattr(AhaBot, "run", fake_run)

    assert aha_main.main() == 0
    assert started["token"] == "dummy-token"
