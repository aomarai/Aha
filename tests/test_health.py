"""Tests for the health/readiness/metrics HTTP server."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import socket

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from aha.bot import AhaBot
from aha.config import Settings
from aha.health import HealthServer
from aha.metrics import ShardState


def _bot(monkeypatch: pytest.MonkeyPatch, shards: list[ShardState]) -> AhaBot:
    bot = AhaBot(Settings(token="test-token"))
    monkeypatch.setattr(bot, "shard_states", lambda: shards)
    return bot


async def _get(bot: AhaBot, path: str) -> tuple[int, str, str]:
    server = HealthServer(bot, "127.0.0.1", 0)
    async with TestClient(TestServer(server.app())) as client:
        response = await client.get(path)
        return response.status, await response.text(), response.headers["Content-Type"]


async def _kill_critical_task(bot: AhaBot, name: str) -> None:
    async def dies() -> None:
        raise RuntimeError("boom")

    await bot.tasks.spawn(dies, name=name, critical=True)


def test_livez_is_ok_when_no_critical_task_has_died(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [])

    status, body, content_type = asyncio.run(_get(bot, "/livez"))

    assert status == 200
    assert json.loads(body) == {"status": "ok"}
    assert content_type == "application/json; charset=utf-8"


def test_livez_fails_and_names_the_dead_critical_task(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [])

    async def scenario() -> tuple[int, str, str]:
        await _kill_critical_task(bot, "gateway-watchdog")
        return await _get(bot, "/livez")

    status, body, _ = asyncio.run(scenario())

    assert status == 503
    assert json.loads(body) == {"status": "unhealthy", "failed_tasks": ["gateway-watchdog"]}


def test_livez_does_not_depend_on_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [ShardState(0, False, math.nan)])

    status, _, _ = asyncio.run(_get(bot, "/livez"))

    assert status == 200


def test_readyz_is_ready_when_every_shard_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [ShardState(0, True, 0.05), ShardState(1, True, 0.06)])

    status, body, _ = asyncio.run(_get(bot, "/readyz"))

    assert status == 200
    assert json.loads(body) == {
        "status": "ready",
        "shards": [{"id": 0, "ready": True}, {"id": 1, "ready": True}],
        "failed_tasks": [],
        "degraded_tasks": [],
    }


def test_readyz_is_unavailable_until_the_shard_is_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [ShardState(0, False, math.nan)])

    status, body, _ = asyncio.run(_get(bot, "/readyz"))

    assert status == 503
    assert json.loads(body) == {
        "status": "not ready",
        "shards": [{"id": 0, "ready": False}],
        "failed_tasks": [],
        "degraded_tasks": [],
    }


def test_readyz_needs_every_owned_shard(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [ShardState(0, True, 0.05), ShardState(1, False, math.nan)])

    status, body, _ = asyncio.run(_get(bot, "/readyz"))

    assert status == 503
    assert json.loads(body)["shards"] == [{"id": 0, "ready": True}, {"id": 1, "ready": False}]


def test_readyz_is_not_ready_without_any_shard(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [])

    status, body, _ = asyncio.run(_get(bot, "/readyz"))

    assert status == 503
    assert json.loads(body)["status"] == "not ready"


def test_readyz_fails_when_a_critical_task_has_died(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [ShardState(0, True, 0.05)])

    async def scenario() -> tuple[int, str, str]:
        await _kill_critical_task(bot, "health-server")
        return await _get(bot, "/readyz")

    status, body, _ = asyncio.run(scenario())

    assert status == 503
    payload = json.loads(body)
    assert payload["status"] == "not ready"
    assert payload["failed_tasks"] == ["health-server"]


def test_metrics_serves_the_prometheus_exposition(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [])
    bot.metrics.record_command("ping", "ok")

    status, body, content_type = asyncio.run(_get(bot, "/metrics"))

    assert status == 200
    assert content_type == "text/plain; version=1.0.0; charset=utf-8"
    assert 'aha_commands_total{command="ping",outcome="ok"} 1.0' in body
    assert 'aha_shard_ready{shard="0"} 0.0' in body  # the bot has not connected
    assert "# HELP aha_shard_ready 1 when the shard is connected and ready." in body


def test_metrics_reflect_the_bots_live_shard_state(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [])
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(type(bot), "latencies", property(lambda self: [(0, 0.042)]))

    _, body, _ = asyncio.run(_get(bot, "/metrics"))

    assert 'aha_shard_ready{shard="0"} 1.0' in body
    assert 'aha_gateway_latency_seconds{shard="0"} 0.042' in body


def test_unknown_paths_and_methods_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot(monkeypatch, [])

    async def scenario() -> tuple[int, int]:
        server = HealthServer(bot, "127.0.0.1", 0)
        async with TestClient(TestServer(server.app())) as client:
            missing = await client.get("/admin")
            wrong_method = await client.post("/livez")
            return missing.status, wrong_method.status

    assert asyncio.run(scenario()) == (404, 405)


def test_run_listens_on_the_requested_address_and_stops_when_cancelled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot(monkeypatch, [ShardState(0, True, 0.05)])
    server = HealthServer(bot, "127.0.0.1", 0)

    async def scenario() -> tuple[int, bool]:
        task = asyncio.create_task(server.run())
        while server.bound_port is None:
            await asyncio.sleep(0.01)
        port = server.bound_port
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{port}/readyz") as response,
        ):
            status = response.status
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        refused = False
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(f"http://127.0.0.1:{port}/readyz"),
            ):
                pass
        except aiohttp.ClientConnectorError:
            refused = True
        return status, refused

    with caplog.at_level(logging.INFO, logger="aha.health"):
        status, refused = asyncio.run(scenario())

    assert status == 200
    assert refused is True  # the socket is closed on cancellation
    assert server.bound_port is not None
    assert [r.getMessage() for r in caplog.records] == [
        f"Health server listening on 127.0.0.1:{server.bound_port}"
    ]


def test_run_surfaces_a_bind_failure_to_the_task_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot(monkeypatch, [])
    server = HealthServer(bot, "256.256.256.256", 0)  # not an address

    with pytest.raises(OSError):
        asyncio.run(server.run())

    assert server.bound_port is None


def test_a_recovering_critical_task_fails_readiness_but_not_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot(monkeypatch, [ShardState(0, True, 0.05)])
    calls: list[int] = []

    async def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        await asyncio.sleep(3600)

    async def scenario() -> tuple[tuple[int, str, str], tuple[int, str, str]]:
        bot.tasks.spawn(flaky, name="gateway-watchdog", restart=True, critical=True)
        for _ in range(10):
            await asyncio.sleep(0)
        live = await _get(bot, "/livez")
        ready = await _get(bot, "/readyz")
        await bot.tasks.close()
        return live, ready

    live, ready = asyncio.run(scenario())

    assert live[0] == 200
    assert ready[0] == 503
    payload = json.loads(ready[1])
    assert payload["status"] == "not ready"
    assert payload["failed_tasks"] == []
    assert payload["degraded_tasks"] == ["gateway-watchdog"]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_run_binds_the_configured_port_and_does_not_log_each_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _bot(monkeypatch, [ShardState(0, True, 0.05)])
    port = _free_port()
    server = HealthServer(bot, "127.0.0.1", port)

    async def scenario() -> int:
        task = asyncio.create_task(server.run())
        while server.bound_port is None:
            await asyncio.sleep(0.01)
        async with (
            aiohttp.ClientSession() as session,
            session.get(f"http://127.0.0.1:{port}/livez") as response,
        ):
            status = response.status
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return status

    with caplog.at_level(logging.DEBUG, logger="aiohttp.access"):
        status = asyncio.run(scenario())

    assert status == 200
    assert server.bound_port == port
    assert [r for r in caplog.records if r.name == "aiohttp.access"] == []  # probes are chatty
