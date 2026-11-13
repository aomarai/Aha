"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


class _IdleHealthServer:
    """Stands in for the real health server so no test binds a TCP port by accident."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args

    async def run(self) -> None:
        await asyncio.Event().wait()


@pytest.fixture(autouse=True)
def _no_real_health_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AhaBot.setup_hook`` starts a health server; tests must not listen on port 8080.

    Tests of the server itself import :class:`aha.health.HealthServer` directly, which this
    patch (of the name inside ``aha.bot``) does not touch.
    """
    monkeypatch.setattr("aha.bot.HealthServer", _IdleHealthServer)
