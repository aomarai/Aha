"""Tests for the Prometheus metrics registry."""

from __future__ import annotations

import asyncio
import math

from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families

from aha.metrics import Metrics, ShardState
from tests.helpers import FakeClock


def _metrics(
    shards: list[ShardState] | None = None,
    guilds: dict[int, int] | None = None,
    clock: FakeClock | None = None,
) -> Metrics:
    return Metrics(
        lambda: shards or [],
        lambda: guilds or {},
        clock=clock or FakeClock(),
    )


def _sample(metrics: Metrics, name: str, labels: dict[str, str]) -> float | None:
    return metrics.registry.get_sample_value(name, labels)


def test_record_command_counts_by_command_and_outcome() -> None:
    metrics = _metrics()

    metrics.record_command("ping", "ok")
    metrics.record_command("ping", "ok")
    metrics.record_command("ping", "cooldown")
    metrics.record_command("roll", "error")

    assert _sample(metrics, "aha_commands_total", {"command": "ping", "outcome": "ok"}) == 2
    assert _sample(metrics, "aha_commands_total", {"command": "ping", "outcome": "cooldown"}) == 1
    assert _sample(metrics, "aha_commands_total", {"command": "roll", "outcome": "error"}) == 1
    assert _sample(metrics, "aha_commands_total", {"command": "roll", "outcome": "ok"}) is None


def test_a_started_timer_records_the_duration() -> None:
    clock = FakeClock(10.0)
    metrics = _metrics(clock=clock)

    async def scenario() -> None:
        metrics.start_timer()
        clock.now += 0.3
        metrics.record_command("ping", "ok")

    asyncio.run(scenario())

    labels = {"command": "ping"}
    assert _sample(metrics, "aha_command_duration_seconds_count", labels) == 1
    assert math.isclose(_sample(metrics, "aha_command_duration_seconds_sum", labels) or 0, 0.3)
    assert _sample(metrics, "aha_command_duration_seconds_bucket", {**labels, "le": "0.25"}) == 0
    assert _sample(metrics, "aha_command_duration_seconds_bucket", {**labels, "le": "0.5"}) == 1


def test_record_command_returns_the_duration_when_a_timer_ran() -> None:
    clock = FakeClock(10.0)
    metrics = _metrics(clock=clock)

    async def scenario() -> float | None:
        metrics.start_timer()
        clock.now += 0.25
        return metrics.record_command("ping", "ok")

    assert asyncio.run(scenario()) == 0.25


def test_without_a_timer_the_command_is_counted_but_no_duration_is_observed() -> None:
    metrics = _metrics()
    returned: list[float | None] = []

    async def scenario() -> None:
        returned.append(metrics.record_command("ping", "ok"))

    asyncio.run(scenario())

    assert returned == [None]

    assert _sample(metrics, "aha_commands_total", {"command": "ping", "outcome": "ok"}) == 1
    assert _sample(metrics, "aha_command_duration_seconds_count", {"command": "ping"}) is None


def test_a_timer_does_not_leak_between_invocations() -> None:
    clock = FakeClock(0.0)
    metrics = _metrics(clock=clock)

    async def first() -> None:
        metrics.start_timer()

    async def second() -> None:
        metrics.record_command("ping", "ok")

    asyncio.run(first())
    asyncio.run(second())

    assert _sample(metrics, "aha_command_duration_seconds_count", {"command": "ping"}) is None


def test_reconnects_are_counted_per_shard() -> None:
    metrics = _metrics()

    metrics.reconnects.labels(shard="0").inc()
    metrics.reconnects.labels(shard="0").inc()
    metrics.reconnects.labels(shard="1").inc()

    assert _sample(metrics, "aha_gateway_reconnects_total", {"shard": "0"}) == 2
    assert _sample(metrics, "aha_gateway_reconnects_total", {"shard": "1"}) == 1


def test_shard_and_guild_state_is_read_at_scrape_time() -> None:
    shards = [ShardState(0, True, 0.05), ShardState(1, False, math.nan)]
    guilds = {0: 12, 1: 3}
    metrics = _metrics(shards, guilds)

    assert _sample(metrics, "aha_shard_ready", {"shard": "0"}) == 1.0
    assert _sample(metrics, "aha_shard_ready", {"shard": "1"}) == 0.0
    assert _sample(metrics, "aha_gateway_latency_seconds", {"shard": "0"}) == 0.05
    latency = _sample(metrics, "aha_gateway_latency_seconds", {"shard": "1"})
    assert latency is not None and math.isnan(latency)
    assert _sample(metrics, "aha_guilds", {"shard": "0"}) == 12
    assert _sample(metrics, "aha_guilds", {"shard": "1"}) == 3

    shards[1] = ShardState(1, True, 0.07)
    guilds[1] = 4

    assert _sample(metrics, "aha_shard_ready", {"shard": "1"}) == 1.0
    assert _sample(metrics, "aha_guilds", {"shard": "1"}) == 4


def test_each_metrics_instance_has_its_own_registry() -> None:
    first, second = _metrics(), _metrics()

    first.record_command("ping", "ok")

    assert first.registry is not second.registry
    assert _sample(second, "aha_commands_total", {"command": "ping", "outcome": "ok"}) is None


def test_the_exposition_text_parses_and_documents_every_metric() -> None:
    metrics = _metrics([ShardState(0, True, 0.05)], {0: 1})
    metrics.record_command("ping", "ok")
    metrics.reconnects.labels(shard="0").inc()

    families = {
        f.name: f
        for f in text_string_to_metric_families(generate_latest(metrics.registry).decode())
    }

    assert {
        "aha_commands",
        "aha_command_duration_seconds",
        "aha_gateway_reconnects",
        "aha_shard_ready",
        "aha_gateway_latency_seconds",
        "aha_guilds",
    } <= set(families)  # counters also expose a ``*_created`` series
    documented = {
        name: family.documentation
        for name, family in families.items()
        if not name.endswith("_created")
    }
    assert documented == {
        "aha_commands": "Command invocations by outcome (ok, error, denied, cooldown).",
        "aha_command_duration_seconds": (
            "Seconds from a command's entry point to its completion or failure."
        ),
        "aha_gateway_reconnects": (
            "Gateway disconnects (each one is followed by a reconnect attempt)."
        ),
        "aha_shard_ready": "1 when the shard is connected and ready.",
        "aha_gateway_latency_seconds": "Gateway heartbeat latency.",
        "aha_guilds": "Guilds served by the shard.",
    }


def test_the_duration_histogram_uses_our_buckets_not_the_library_defaults() -> None:
    clock = FakeClock(0.0)
    metrics = _metrics(clock=clock)

    async def scenario() -> None:
        metrics.start_timer()
        metrics.record_command("ping", "ok")

    asyncio.run(scenario())

    buckets = {"0.005", "0.01", "0.025", "0.05", "0.1", "0.25", "0.5", "1.0", "2.5", "5.0", "10.0"}
    seen = {
        sample.labels["le"]
        for family in metrics.registry.collect()
        for sample in family.samples
        if sample.name == "aha_command_duration_seconds_bucket"
    }
    assert seen == buckets | {"+Inf"}  # the library defaults would add 0.075, 0.75, 7.5, ...
