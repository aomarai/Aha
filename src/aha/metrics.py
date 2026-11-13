"""Prometheus metrics for the bot, kept in a per-bot registry.

A private :class:`~prometheus_client.CollectorRegistry` (not the process-global default) keeps
tests and multiple bots from colliding on metric names. Counters that need an event (commands,
reconnects) are plain ``prometheus_client`` metrics; values that are simply *state* of the bot
(shard readiness, latency, guild counts) are read at scrape time by :class:`_BotCollector`, so
they can never drift from the bot's real state.

Commands are counted in one place, :class:`aha.cogs.observability.Observability`, never scattered
through the cogs.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

__all__ = ["Metrics", "ShardState"]

#: Seconds between a command's entry point and its completion or failure.
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

_started: ContextVar[float | None] = ContextVar("aha_command_started", default=None)


@dataclass(frozen=True, slots=True)
class ShardState:
    """A snapshot of one gateway shard owned by this process."""

    shard_id: int
    #: Connected and finished its initial guild download (discord.py's "ready").
    ready: bool
    #: Heartbeat latency in seconds (``nan`` until the first heartbeat).
    latency: float


class _BotCollector(Collector):
    """Reads shard and guild state from the bot each time Prometheus scrapes."""

    def __init__(
        self,
        shards: Callable[[], Iterable[ShardState]],
        guilds: Callable[[], Mapping[int, int]],
    ) -> None:
        self._shards = shards
        self._guilds = guilds

    def collect(self) -> Iterable[Metric]:
        ready = GaugeMetricFamily(
            "aha_shard_ready", "1 when the shard is connected and ready.", labels=["shard"]
        )
        latency = GaugeMetricFamily(
            "aha_gateway_latency_seconds", "Gateway heartbeat latency.", labels=["shard"]
        )
        for state in self._shards():
            ready.add_metric([str(state.shard_id)], 1.0 if state.ready else 0.0)
            latency.add_metric([str(state.shard_id)], state.latency)
        guilds = GaugeMetricFamily("aha_guilds", "Guilds served by the shard.", labels=["shard"])
        for shard_id, count in sorted(self._guilds().items()):
            guilds.add_metric([str(shard_id)], float(count))
        return [ready, latency, guilds]


class Metrics:
    """The bot's metrics registry and the handful of event-driven instruments."""

    def __init__(
        self,
        shards: Callable[[], Iterable[ShardState]],
        guilds: Callable[[], Mapping[int, int]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.registry = CollectorRegistry()
        self._clock = clock
        self.commands = Counter(
            "aha_commands_total",
            "Command invocations by outcome (ok, error, denied, cooldown).",
            ["command", "outcome"],
            registry=self.registry,
        )
        self.command_duration = Histogram(
            "aha_command_duration_seconds",
            "Seconds from a command's entry point to its completion or failure.",
            ["command"],
            buckets=_DURATION_BUCKETS,
            registry=self.registry,
        )
        self.reconnects = Counter(
            "aha_gateway_reconnects_total",
            "Gateway disconnects (each one is followed by a reconnect attempt).",
            ["shard"],
            registry=self.registry,
        )
        self.scheduler_lag = Gauge(
            "aha_scheduler_lag_seconds",
            "Seconds the oldest due scheduled job has been waiting to be claimed.",
            registry=self.registry,
        )
        self.registry.register(_BotCollector(shards, guilds))

    def start_timer(self) -> None:
        """Mark the start of a command in the current invocation's context."""
        _started.set(self._clock())

    def record_command(self, command: str, outcome: str) -> float | None:
        """Count one finished command and observe how long it took.

        Returns the duration in seconds, or ``None`` when no timer was started for this
        invocation.
        """
        self.commands.labels(command=command, outcome=outcome).inc()
        started = _started.get()
        if started is None:
            return None
        duration = self._clock() - started
        self.command_duration.labels(command=command).observe(duration)
        return duration
