"""Tests for command metrics, readiness state, and the health-server wiring in the bot."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, ClassVar, cast

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from aha import logctx
from aha.bot import HEALTH_TASK, READINESS_DISCONNECT_GRACE, AhaBot
from aha.cogs.observability import AUDIT_LOGGER, Observability, outcome_of
from aha.cogs.observability import setup as observability_setup
from aha.config import Settings
from aha.dbmigrate import SchemaError, load_migrations
from aha.guildsettings import GuildSettingsCache
from aha.logging_setup import JsonFormatter
from aha.metrics import ShardState
from tests.helpers import (
    Named,
    StubContext,
    StubInteraction,
    as_context,
    as_interaction,
    as_tree_interaction,
)


def _bot(**settings: Any) -> AhaBot:
    return AhaBot(Settings(token="test-token", **settings))


def _count(bot: AhaBot, command: str, outcome: str) -> float | None:
    return bot.metrics.registry.get_sample_value(
        "aha_commands_total", {"command": command, "outcome": outcome}
    )


def test_a_completed_context_menu_is_counted_and_audited(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = _bot()
    menu = app_commands.ContextMenu(name="Count reactions", callback=_menu_callback)

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        asyncio.run(
            Observability(bot).on_app_command_completion(as_interaction(StubInteraction()), menu)
        )

    assert _count(bot, "Count reactions", "ok") == 1
    (record,) = caplog.records
    assert record.getMessage() == "Command /Count reactions completed"
    assert record.__dict__["arguments"] == []


def test_a_hybrid_commands_app_completion_is_not_counted_twice() -> None:
    bot = _bot()
    hybrid_app_command = cast("Any", Named(qualified_name="ping"))

    asyncio.run(
        Observability(bot).on_app_command_completion(
            as_interaction(StubInteraction()), hybrid_app_command
        )
    )

    assert _count(bot, "ping", "ok") is None


async def _menu_callback(interaction: discord.Interaction, message: discord.Message) -> None:
    return None


def test_a_completed_command_is_counted_ok() -> None:
    bot = _bot()

    asyncio.run(Observability(bot).on_command_completion(as_context(StubContext())))

    assert _count(bot, "ping", "ok") == 1


def test_a_completion_without_a_command_is_ignored() -> None:
    bot = _bot()

    asyncio.run(Observability(bot).on_command_completion(as_context(StubContext(command=False))))

    assert bot.metrics.registry.get_sample_value("aha_commands_total", {}) is None


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (commands.CommandOnCooldown(cast("Any", None), 1.0, commands.BucketType.user), "cooldown"),
        (app_commands.CommandOnCooldown(cast("Any", None), 1.0), "cooldown"),
        (commands.MissingPermissions(["manage_guild"]), "denied"),
        (app_commands.MissingPermissions(["manage_guild"]), "denied"),
        (commands.NotOwner(), "denied"),
        (commands.NoPrivateMessage(), "denied"),
        (commands.BadArgument("nope"), "error"),
        (RuntimeError("boom"), "error"),
    ],
)
def test_outcome_of_classifies_failures(error: Exception, outcome: str) -> None:
    assert outcome_of(error) == outcome


def test_outcome_of_looks_through_wrapper_exceptions() -> None:
    command = cast("Any", Named(name="x", qualified_name="x"))

    assert outcome_of(commands.CommandInvokeError(RuntimeError("boom"))) == "error"
    assert (
        outcome_of(commands.HybridCommandError(app_commands.MissingPermissions(["manage_guild"])))
        == "denied"
    )
    assert outcome_of(app_commands.CommandInvokeError(command, RuntimeError("boom"))) == "error"
    nested = app_commands.CommandInvokeError(
        command, commands.CommandOnCooldown(cast("Any", None), 1.0, commands.BucketType.user)
    )
    assert outcome_of(nested) == "cooldown"


def test_a_failed_message_or_hybrid_command_is_counted_by_outcome() -> None:
    bot = _bot()
    cog = Observability(bot)

    asyncio.run(cog.on_command_error(as_context(StubContext()), commands.NotOwner()))
    asyncio.run(cog.on_command_error(as_context(StubContext()), commands.BadArgument("x")))

    assert _count(bot, "ping", "denied") == 1
    assert _count(bot, "ping", "error") == 1
    assert _count(bot, "ping", "ok") is None


def test_an_unknown_command_is_not_counted() -> None:
    bot = _bot()
    ctx = StubContext(command=False)

    asyncio.run(Observability(bot).on_command_error(as_context(ctx), commands.CommandNotFound()))

    assert bot.metrics.registry.get_sample_value("aha_commands_total", {}) is None


def test_a_tree_level_failure_is_counted_with_its_command() -> None:
    bot = _bot()
    error = app_commands.CommandOnCooldown(cast("Any", None), 3.0)

    asyncio.run(Observability(bot).on_app_command_error(as_interaction(StubInteraction()), error))

    assert _count(bot, "ping", "cooldown") == 1


def test_a_tree_level_failure_without_a_command_is_ignored() -> None:
    bot = _bot()
    interaction = StubInteraction(command=False)
    error = app_commands.CommandOnCooldown(cast("Any", None), 3.0)

    asyncio.run(Observability(bot).on_app_command_error(as_interaction(interaction), error))

    assert bot.metrics.registry.get_sample_value("aha_commands_total", {}) is None


def test_the_rate_limit_rejection_of_a_slash_command_is_counted_with_a_duration() -> None:
    bot = AhaBot(Settings(token="test-token"), rate_limit=(1, 60.0))
    cog = Observability(bot)
    interaction = as_tree_interaction(StubInteraction())

    async def scenario() -> None:
        await bot.tree.interaction_check(interaction)
        with pytest.raises(app_commands.CommandOnCooldown) as caught:
            await bot.tree.interaction_check(interaction)
        await cog.on_app_command_error(interaction, caught.value)

    asyncio.run(scenario())

    assert _count(bot, "ping", "cooldown") == 1
    assert (
        bot.metrics.registry.get_sample_value(
            "aha_command_duration_seconds_count", {"command": "ping"}
        )
        == 1
    )


def test_a_mention_command_gets_a_duration_from_the_bot_check() -> None:
    bot = _bot()
    ctx = StubContext()
    cog = Observability(bot)

    async def scenario() -> None:
        await bot._enforce_rate_limit(as_context(ctx))
        await cog.on_command_completion(as_context(ctx))

    asyncio.run(scenario())

    assert _count(bot, "ping", "ok") == 1
    assert (
        bot.metrics.registry.get_sample_value(
            "aha_command_duration_seconds_count", {"command": "ping"}
        )
        == 1
    )


def test_setup_registers_the_cog_and_its_listeners_on_a_real_bot() -> None:
    bot = _bot()

    asyncio.run(observability_setup(bot))

    cog = bot.get_cog("Observability")
    assert isinstance(cog, Observability)
    assert cog.bot is bot
    for event in ("on_command_completion", "on_command_error", "on_app_command_error"):
        assert len(bot.extra_events[event]) == 1


def test_setup_hook_loads_the_observability_cog(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()

    async def fake_super_close(self: Any) -> None:
        return None

    monkeypatch.setattr(commands.Bot, "close", fake_super_close)

    async def scenario() -> None:
        await bot.setup_hook()  # loads every real cog; production mode does not sync
        await bot.close()

    asyncio.run(scenario())

    # ``load_extension`` imports its own copy of the module, so compare by name, not class.
    assert bot.get_cog("Observability") is not None
    assert len(bot.extra_events["on_command_completion"]) == 1


def test_shard_states_report_not_ready_before_the_gateway_is_ready() -> None:
    bot = _bot()

    (state,) = bot.shard_states()

    assert state.shard_id == 0
    assert state.ready is False
    assert math.isnan(state.latency)


def test_shard_states_report_ready_with_the_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(type(bot), "latency", property(lambda self: 0.042))

    assert bot.shard_states() == [ShardState(0, True, 0.042)]


def test_a_short_disconnect_keeps_the_shard_ready_but_a_long_one_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    bot._disconnected_since = 100.0

    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 100.0 + READINESS_DISCONNECT_GRACE)
    assert bot.shard_states()[0].ready is True  # exactly at the grace limit

    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 100.0 + READINESS_DISCONNECT_GRACE + 0.5)
    assert bot.shard_states()[0].ready is False


def test_a_fresh_ready_ends_a_long_outage_so_the_shard_reports_ready_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    monkeypatch.setattr(bot, "is_ready", lambda: True)
    monkeypatch.setattr(type(bot), "user", property(lambda self: Named(id=1)))
    bot._disconnected_since = 100.0
    monkeypatch.setattr("aha.bot.time.monotonic", lambda: 100.0 + 10 * READINESS_DISCONNECT_GRACE)
    assert bot.shard_states()[0].ready is False  # a long outage

    asyncio.run(bot.on_ready())  # discord could not resume and started a fresh session

    assert bot.shard_states()[0].ready is True


def test_a_connected_shard_that_is_not_ready_stays_unready_after_a_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = _bot()
    monkeypatch.setattr(bot, "is_ready", lambda: False)

    assert bot.shard_states()[0].ready is False


def test_guild_counts_group_cached_guilds_by_shard(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _bot()
    guilds = [Named(shard_id=0), Named(shard_id=0), Named(shard_id=2)]
    monkeypatch.setattr(type(bot), "guilds", property(lambda self: guilds))

    assert bot.guild_counts() == {0: 2, 2: 1}


def test_an_owned_shard_without_guilds_reports_zero_rather_than_no_sample() -> None:
    bot = _bot()

    assert bot.guild_counts() == {0: 0}
    assert bot.metrics.registry.get_sample_value("aha_guilds", {"shard": "0"}) == 0


def test_a_disconnect_is_counted_as_a_reconnect() -> None:
    bot = _bot()

    asyncio.run(bot.on_disconnect())
    asyncio.run(bot.on_disconnect())

    assert (
        bot.metrics.registry.get_sample_value("aha_gateway_reconnects_total", {"shard": "0"}) == 2
    )


class _RecordingServer:
    """Replaces ``HealthServer`` to see how ``setup_hook`` constructs and starts it."""

    created: ClassVar[list[tuple[AhaBot, str, int]]] = []

    def __init__(self, bot: AhaBot, host: str, port: int) -> None:
        _RecordingServer.created.append((bot, host, port))

    async def run(self) -> None:
        await asyncio.Event().wait()


def _setup_hook(bot: AhaBot, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_super_close(self: Any) -> None:
        return None

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(commands.Bot, "close", fake_super_close)
    names: list[str] = []

    async def scenario() -> None:
        await bot.setup_hook()
        await bot.setup_hook()  # a second call must not start a second server
        names.extend(sorted(name for name in (HEALTH_TASK,) if name in bot.tasks))
        await bot.close()

    asyncio.run(scenario())
    return names


def test_setup_hook_starts_the_health_server_with_the_configured_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingServer.created.clear()
    monkeypatch.setattr("aha.bot.HealthServer", _RecordingServer)
    bot = _bot(health_bind="0.0.0.0", health_port=9100)

    running = _setup_hook(bot, monkeypatch)

    assert _RecordingServer.created == [(bot, "0.0.0.0", 9100)]
    assert running == [HEALTH_TASK]


def test_setup_hook_skips_the_health_server_when_the_bot_is_built_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingServer.created.clear()
    monkeypatch.setattr("aha.bot.HealthServer", _RecordingServer)
    bot = AhaBot(Settings(token="test-token", health_port=9100), health_server=False)

    running = _setup_hook(bot, monkeypatch)

    assert bot.health_server_enabled is False
    assert _RecordingServer.created == []
    assert running == []


def test_setup_hook_skips_the_health_server_when_the_port_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingServer.created.clear()
    monkeypatch.setattr("aha.bot.HealthServer", _RecordingServer)
    bot = _bot(health_port=0)

    running = _setup_hook(bot, monkeypatch)

    assert _RecordingServer.created == []
    assert running == []


def test_started_at_is_the_time_the_bot_was_created(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("aha.bot.time.time", lambda: 1_700_000_123.5)

    assert _bot().started_at == 1_700_000_123.5


def test_a_bot_started_for_real_has_a_current_start_time() -> None:
    before = time.time()
    bot = _bot()

    assert before <= bot.started_at <= time.time()


class _CrashingOnceServer:
    """A health server whose first run crashes, as a failed bind would."""

    runs = 0

    def __init__(self, bot: AhaBot, host: str, port: int) -> None:
        pass

    async def run(self) -> None:
        _CrashingOnceServer.runs += 1
        if _CrashingOnceServer.runs == 1:
            raise OSError("address already in use")
        await asyncio.Event().wait()


def test_the_health_server_is_a_critical_restartable_task(monkeypatch: pytest.MonkeyPatch) -> None:
    _CrashingOnceServer.runs = 0
    monkeypatch.setattr("aha.bot.HealthServer", _CrashingOnceServer)
    bot = _bot()

    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_super_close(self: Any) -> None:
        return None

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(commands.Bot, "close", fake_super_close)

    async def scenario() -> tuple[frozenset[str], frozenset[str], int]:
        await bot.setup_hook()
        for _ in range(10):
            await asyncio.sleep(0)
        result = (bot.tasks.degraded, bot.tasks.failed, _CrashingOnceServer.runs)
        await bot.close()
        return result

    degraded, failed, runs = asyncio.run(scenario())

    # Critical and restartable: parked in back-off (degraded, so readiness reports it), not dead.
    assert degraded == frozenset({HEALTH_TASK})
    assert failed == frozenset()
    assert runs == 1


def test_a_completed_command_is_written_to_the_audit_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = _bot()
    ctx = StubContext()
    ctx.kwargs = {"sides": 6, "label": "dice"}

    async def scenario() -> None:
        bot.metrics._clock = lambda: 100.0
        bot.metrics.start_timer()
        bot.metrics._clock = lambda: 100.0126
        await Observability(bot).on_command_completion(as_context(ctx))

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        asyncio.run(scenario())

    (record,) = caplog.records
    assert record.name == "aha.audit" == AUDIT_LOGGER
    assert record.levelno == logging.INFO
    assert record.getMessage() == "Command /ping completed"
    assert record.__dict__["event"] == "command"
    assert record.__dict__["outcome"] == "ok"
    assert record.__dict__["arguments"] == ["label", "sides"]  # names only, sorted
    assert record.__dict__["duration_ms"] == 13


def test_the_audit_duration_is_in_whole_milliseconds(caplog: pytest.LogCaptureFixture) -> None:
    bot = _bot()
    clock = [100.0]
    bot.metrics._clock = lambda: clock[0]

    async def scenario() -> None:
        bot.metrics.start_timer()
        clock[0] = 102.0  # exactly two seconds later
        await Observability(bot).on_command_completion(as_context(StubContext()))

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        asyncio.run(scenario())

    assert caplog.records[0].__dict__["duration_ms"] == 2000


def test_the_audit_log_has_no_duration_without_a_timer(caplog: pytest.LogCaptureFixture) -> None:
    bot = _bot()

    async def scenario() -> None:
        await Observability(bot).on_command_completion(as_context(StubContext()))

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        asyncio.run(scenario())

    (record,) = caplog.records
    assert record.__dict__["duration_ms"] is None
    assert record.__dict__["arguments"] == []


def test_the_audit_log_never_contains_argument_values_or_the_who_in_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = _bot()
    ctx = StubContext()
    ctx.kwargs = {"text": "my private reminder", "link": "https://example.test/secret"}

    async def scenario() -> None:
        await Observability(bot).on_command_completion(as_context(ctx))

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        asyncio.run(scenario())

    (record,) = caplog.records
    line = JsonFormatter().format(record)
    assert "my private reminder" not in line
    assert "example.test" not in line
    assert json.loads(line)["arguments"] == ["link", "text"]


def test_the_json_audit_line_carries_the_invocation_context() -> None:
    lines: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(JsonFormatter())
    audit_logger = logging.getLogger(AUDIT_LOGGER)
    previous_level = audit_logger.level
    audit_logger.addHandler(handler)
    audit_logger.setLevel(logging.INFO)
    bot = _bot()
    ctx = StubContext()

    async def scenario() -> None:
        logctx.bind_context(as_context(ctx))
        await Observability(bot).on_command_completion(as_context(ctx))

    try:
        asyncio.run(scenario())
    finally:
        audit_logger.removeHandler(handler)
        audit_logger.setLevel(previous_level)

    (line,) = lines
    payload = json.loads(line)
    assert payload["logger"] == "aha.audit"
    assert payload["msg"] == "Command /ping completed"
    assert (payload["shard_id"], payload["guild_id"], payload["channel_id"]) == (2, 11, 22)
    assert (payload["user_id"], payload["command"]) == (33, "ping")
    assert payload["event"] == "command"
    assert payload["outcome"] == "ok"


def test_failures_and_unknown_commands_are_not_audited_as_completions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = _bot()
    cog = Observability(bot)

    async def scenario() -> None:
        await cog.on_command_error(as_context(StubContext()), commands.NotOwner())
        await cog.on_command_completion(as_context(StubContext(command=False)))

    with caplog.at_level(logging.DEBUG, logger=AUDIT_LOGGER):
        asyncio.run(scenario())

    assert caplog.records == []


# ----------------------------------------------------------------------- database lifecycle


class _FakeDatabase:
    """Stands in for ``aha.db.Database``: records connect arguments, acquires and closing."""

    instances: ClassVar[list[_FakeDatabase]] = []

    def __init__(self) -> None:
        self.closed = False
        self.kwargs: dict[str, Any] = {}
        self.url = ""

    @classmethod
    async def connect(cls, url: str, **kwargs: Any) -> _FakeDatabase:
        database = cls()
        database.url = url
        database.kwargs = kwargs
        cls.instances.append(database)
        return database

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[str]:
        yield "connection"

    async def close(self) -> None:
        self.closed = True


def _database_bot(monkeypatch: pytest.MonkeyPatch, **settings: Any) -> AhaBot:
    _FakeDatabase.instances.clear()
    monkeypatch.setattr("aha.bot.Database", _FakeDatabase)
    bot = AhaBot(Settings(token="test-token", health_port=0, **settings))

    async def fake_load_extension(name: str) -> None:
        return None

    async def fake_super_close(self: Any) -> None:
        return None

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)
    monkeypatch.setattr(commands.AutoShardedBot, "close", fake_super_close)
    return bot


def test_without_a_database_url_the_bot_has_no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _database_bot(monkeypatch)

    async def scenario() -> None:
        await bot.setup_hook()
        await bot.close()

    asyncio.run(scenario())

    assert bot.db is None
    assert _FakeDatabase.instances == []


def test_a_database_url_opens_a_pool_checks_the_schema_and_closes_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    checked: list[tuple[object, object]] = []

    async def fake_require_current(connection: object, migrations: object) -> None:
        checked.append((connection, migrations))

    monkeypatch.setattr("aha.bot.require_current", fake_require_current)
    bot = _database_bot(
        monkeypatch,
        database_url="postgresql://u:pw@db/aha",
        db_pool_max=9,
        db_statement_timeout=1.5,
    )

    async def scenario() -> None:
        await bot.setup_hook()
        assert cast("Any", bot.db) is _FakeDatabase.instances[0]
        await bot.close()

    with caplog.at_level(logging.INFO, logger="aha.bot"):
        asyncio.run(scenario())

    (database,) = _FakeDatabase.instances
    assert database.url == "postgresql://u:pw@db/aha"
    assert database.kwargs == {"max_size": 9, "statement_timeout": 1.5}
    assert database.closed is True
    settings = cast("Any", bot.guild_settings)
    assert isinstance(settings, GuildSettingsCache)
    assert cast("Any", settings._store)._db is database
    assert [connection for connection, _ in checked] == ["connection"]
    assert [migrations for _, migrations in checked] == [load_migrations()]
    assert load_migrations()  # the packaged migrations really were passed, not an empty list
    assert "Connected to the database; the schema is up to date" in [
        record.getMessage() for record in caplog.records
    ]


def test_a_schema_that_is_behind_stops_startup_and_the_pool_is_still_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def behind(connection: object, migrations: object) -> None:
        raise SchemaError("The database schema is behind (pending: 0002). Run 'aha migrate'.")

    monkeypatch.setattr("aha.bot.require_current", behind)
    bot = _database_bot(monkeypatch, database_url="postgresql://db/aha")

    async def scenario() -> None:
        with pytest.raises(SchemaError, match="Run 'aha migrate'"):
            await bot.setup_hook()
        await bot.close()  # what ``async with bot`` does when startup fails

    asyncio.run(scenario())

    assert _FakeDatabase.instances[0].closed is True


def test_the_sync_job_never_opens_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeDatabase.instances.clear()
    monkeypatch.setattr("aha.bot.Database", _FakeDatabase)
    bot = AhaBot(
        Settings(token="t", health_port=0, database_url="postgresql://db/aha"), use_database=False
    )

    async def fake_load_extension(name: str) -> None:
        return None

    monkeypatch.setattr(bot, "load_extension", fake_load_extension)

    asyncio.run(bot.setup_hook())

    assert bot.db is None
    assert _FakeDatabase.instances == []


def test_a_database_gives_the_bot_a_scheduler_that_runs_as_a_critical_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aha.bot import SCHEDULER_TASK

    async def current(connection: object, migrations: object) -> None:
        return None

    monkeypatch.setattr("aha.bot.require_current", current)
    monkeypatch.setattr("aha.bot.JobStore", lambda database: ("store", database))
    bot = _database_bot(monkeypatch, database_url="postgresql://db/aha")

    async def scenario() -> tuple[object, bool]:
        await bot.setup_hook()
        running = SCHEDULER_TASK in bot.tasks
        await bot.close()
        return bot.scheduler, running

    scheduler, running = asyncio.run(scenario())

    assert running is True
    assert scheduler is not None
    args = cast("Any", scheduler).args
    assert args == (("store", bot.db),)
    bot.metrics.scheduler_lag.set(0)  # the lag callback is the metric's setter
    assert cast("Any", scheduler).kwargs["on_lag"] == bot.metrics.scheduler_lag.set


def test_without_a_database_there_is_no_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    from aha.bot import SCHEDULER_TASK

    bot = _database_bot(monkeypatch)

    async def scenario() -> bool:
        await bot.setup_hook()
        present = SCHEDULER_TASK in bot.tasks
        await bot.close()
        return present

    assert asyncio.run(scenario()) is False
    assert bot.scheduler is None


class _CrashingScheduler:
    """A scheduler whose first run crashes, to show how the bot supervises it."""

    runs = 0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def run(self) -> None:
        _CrashingScheduler.runs += 1
        if _CrashingScheduler.runs == 1:
            raise RuntimeError("scheduler bug")
        await asyncio.Event().wait()


def test_the_scheduler_is_a_critical_restartable_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from aha.bot import SCHEDULER_TASK

    async def current(connection: object, migrations: object) -> None:
        return None

    _CrashingScheduler.runs = 0
    monkeypatch.setattr("aha.bot.require_current", current)
    monkeypatch.setattr("aha.bot.Scheduler", _CrashingScheduler)
    bot = _database_bot(monkeypatch, database_url="postgresql://db/aha")

    async def scenario() -> tuple[frozenset[str], frozenset[str], int]:
        await bot.setup_hook()
        for _ in range(10):
            await asyncio.sleep(0)
        result = (bot.tasks.degraded, bot.tasks.failed, _CrashingScheduler.runs)
        await bot.close()
        return result

    degraded, failed, runs = asyncio.run(scenario())

    # Critical and restartable: parked in back-off (readiness reports it), not dead.
    assert degraded == frozenset({SCHEDULER_TASK})
    assert failed == frozenset()
    assert runs == 1
