"""Offline tests for the /config and /configure cog and the departed-guild purge."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import discord
import pytest

from aha.cogs import config as config_module
from aha.cogs.config import PURGE_DELAY, PURGE_KIND, Config
from aha.cogs.config import setup as config_setup
from aha.guildsettings import GuildSettings
from aha.jobstore import Job
from tests.helpers import Named, StubContext, as_context

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class FakeSettings:
    """Records writes; ``get`` serves ``current``."""

    def __init__(self, current: GuildSettings | None = None) -> None:
        self.current = current or GuildSettings(11)
        self.calls: list[tuple[Any, ...]] = []

    async def get(self, guild_id: int) -> GuildSettings:
        self.calls.append(("get", guild_id))
        return self.current

    async def set_log_channel(self, guild_id: int, channel_id: int | None, by: int) -> None:
        self.calls.append(("log", guild_id, channel_id, by))

    async def set_summaries(self, guild_id: int, enabled: bool, by: int) -> None:
        self.calls.append(("summaries", guild_id, enabled, by))

    async def disable(self, guild_id: int, command: str, by: int) -> None:
        self.calls.append(("disable", guild_id, command, by))

    async def enable(self, guild_id: int, command: str, by: int) -> None:
        self.calls.append(("enable", guild_id, command, by))

    async def delete(self, guild_id: int) -> bool:
        self.calls.append(("delete", guild_id))
        return True


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, datetime, dict[str, Any]]] = []
        self.purged: list[int] = []

    async def enqueue(self, kind: str, run_at: datetime, **kwargs: Any) -> int:
        self.enqueued.append((kind, run_at, kwargs))
        return 1

    async def purge_guild(self, guild_id: int) -> int:
        self.purged.append(guild_id)
        return 0


class FakeChannel(discord.abc.Messageable):
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id

    async def _get_channel(self) -> Any:  # pragma: no cover - never reached
        raise NotImplementedError


class FakeBot:
    def __init__(self, settings: FakeSettings | None, *, db: Any = None) -> None:
        self.guild_settings = settings
        self.db = db
        self.scheduler: Any = None
        self.guilds: dict[int, Any] = {}
        self.cogs: list[Any] = []
        self.known_commands = {"roll", "coin", "config", "help"}

    def get_guild(self, guild_id: int) -> Any:
        return self.guilds.get(guild_id)

    def get_command(self, name: str) -> Any:
        return Named(name=name) if name in self.known_commands else None

    async def add_cog(self, cog: Any) -> None:
        self.cogs.append(cog)


def make(settings: FakeSettings | None = None, jobs: FakeJobs | None = None) -> Config:
    bot = FakeBot(settings)
    return Config(cast("Any", bot), cast("Any", jobs))


def ctx_for(bot: FakeBot, *, channels: dict[int, Any] | None = None) -> StubContext:
    ctx = StubContext()
    ctx.bot = bot
    ctx.guild = Named(id=11, get_channel=lambda cid: (channels or {}).get(cid))
    return ctx


def configure(cog: Config, ctx: StubContext, setting: str, value: str) -> None:
    asyncio.run(cast("Any", cog.configure.callback)(cog, as_context(ctx), setting, value))


def text(ctx: StubContext) -> str:
    ((_, kwargs),) = ctx.replies
    assert kwargs["ephemeral"] is True
    return cast("str", kwargs["embed"].description)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "utcnow", lambda: NOW)


# ---------------------------------------------------------------------------- /config


def test_config_shows_the_defaults() -> None:
    settings = FakeSettings()
    cog = make(settings)
    ctx = ctx_for(cast("Any", cog.bot))

    asyncio.run(cast("Any", cog.config.callback)(cog, as_context(ctx)))

    assert text(ctx) == "**Log channel:** not set\n**Disabled commands:** none\n**Summaries:** off"
    assert settings.calls == [("get", 11)]


def test_config_shows_what_is_set() -> None:
    settings = FakeSettings(
        GuildSettings(11, disabled_commands=frozenset({"roll", "coin"}), log_channel_id=99)
    )
    cog = make(settings)
    ctx = ctx_for(cast("Any", cog.bot))

    asyncio.run(cast("Any", cog.config.callback)(cog, as_context(ctx)))

    assert (
        text(ctx)
        == "**Log channel:** <#99>\n**Disabled commands:** `coin`, `roll`\n**Summaries:** off"
    )


def test_config_without_a_database_says_so() -> None:
    cog = make(None)
    ctx = ctx_for(cast("Any", cog.bot))

    asyncio.run(cast("Any", cog.config.callback)(cog, as_context(ctx)))

    assert text(ctx) == "Server settings are not available: no database is configured."


# ----------------------------------------------------------------------- /configure


def test_the_commands_require_manage_server_on_the_server_side() -> None:
    for command in (Config.config, Config.configure):
        names = {getattr(check, "__qualname__", "") for check in command.checks}
        assert any("has_permissions" in name for name in names), command.name
        assert any("guild_only" in name for name in names), command.name
        assert command.extras["category"] == "Admin"


def test_log_channel_accepts_a_mention_or_an_id(caplog: pytest.LogCaptureFixture) -> None:
    for value in ("<#55>", "55", "  55  "):
        settings = FakeSettings()
        cog = make(settings)
        ctx = ctx_for(cast("Any", cog.bot), channels={55: FakeChannel(55)})

        with caplog.at_level(logging.INFO, logger="aha.audit"):
            configure(cog, ctx, "log-channel", value)

        assert settings.calls == [("log", 11, 55, 33)]
        assert text(ctx) == "Log channel set."
    record = caplog.records[-1]
    assert record.getMessage() == "Server setting changed"
    assert (record.__dict__["event"], record.__dict__["setting"]) == (
        "config.changed",
        "log-channel",
    )


def test_log_channel_off_clears_it() -> None:
    settings = FakeSettings()
    cog = make(settings)
    ctx = ctx_for(cast("Any", cog.bot))

    configure(cog, ctx, "log-channel", "OFF")

    assert settings.calls == [("log", 11, None, 33)]
    assert text(ctx) == "Log channel cleared."


@pytest.mark.parametrize("value", ["general", "<#abc>", "77", "1" * 25])
def test_log_channel_rejects_anything_that_is_not_a_channel_here(value: str) -> None:
    settings = FakeSettings()
    cog = make(settings)
    ctx = ctx_for(cast("Any", cog.bot), channels={88: Named(id=88)})  # 88 is not messageable

    configure(cog, ctx, "log-channel", value)

    assert settings.calls == []
    assert text(ctx) == "Give a text channel in this server, or `off`."


def test_a_non_messageable_channel_is_rejected() -> None:
    cog = make(FakeSettings())
    ctx = ctx_for(cast("Any", cog.bot), channels={88: Named(id=88)})

    configure(cog, ctx, "log-channel", "88")

    assert text(ctx) == "Give a text channel in this server, or `off`."


def test_disable_and_enable_normalise_the_name() -> None:
    for setting, call_name, message in (
        ("disable", "disable", "`roll` is now disabled in this server."),
        ("enable", "enable", "`roll` is enabled in this server."),
    ):
        settings = FakeSettings()
        cog = make(settings)
        ctx = ctx_for(cast("Any", cog.bot))

        configure(cog, ctx, setting, " /ROLL ")

        assert settings.calls == [(call_name, 11, "roll", 33)]
        assert text(ctx) == message


@pytest.mark.parametrize("name", ["config", "configure", "help"])
def test_protected_commands_cannot_be_disabled(name: str) -> None:
    settings = FakeSettings()
    cog = make(settings)
    ctx = ctx_for(cast("Any", cog.bot))

    configure(cog, ctx, "disable", name)

    assert settings.calls == []
    assert text(ctx) == f"`{name}` can't be disabled."


def test_an_unknown_command_is_refused() -> None:
    settings = FakeSettings()
    cog = make(settings)
    ctx = ctx_for(cast("Any", cog.bot))

    configure(cog, ctx, "disable", "nope")

    assert settings.calls == []
    assert text(ctx) == "There is no command `nope`."


def test_configure_without_a_database_says_so() -> None:
    cog = make(None)
    ctx = ctx_for(cast("Any", cog.bot))

    configure(cog, ctx, "disable", "roll")

    assert text(ctx) == "Server settings are not available: no database is configured."


def test_configure_outside_a_guild_is_refused() -> None:
    settings = FakeSettings()
    cog = make(settings)
    ctx = ctx_for(cast("Any", cog.bot))
    ctx.guild = None

    configure(cog, ctx, "disable", "roll")

    assert settings.calls == []
    assert text(ctx) == "Server settings are not available: no database is configured."


# ------------------------------------------------------------------ departed guilds


def test_leaving_a_guild_schedules_the_deletion_of_its_data() -> None:
    jobs = FakeJobs()
    cog = make(FakeSettings(), jobs)

    asyncio.run(cog.on_guild_remove(cast("Any", Named(id=11))))

    assert jobs.enqueued == [(PURGE_KIND, NOW + PURGE_DELAY, {"guild_id": 11})]
    assert timedelta(days=30) == PURGE_DELAY
    assert PURGE_KIND == "guild_purge"


def test_leaving_a_guild_without_a_database_schedules_nothing() -> None:
    cog = make(FakeSettings(), None)

    asyncio.run(cog.on_guild_remove(cast("Any", Named(id=11))))  # must not raise


def purge_job(guild_id: int | None = 11) -> Job:
    return Job(1, PURGE_KIND, NOW, None, guild_id, {}, 1)


def test_the_purge_deletes_settings_and_jobs_of_a_guild_that_stayed_gone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings, jobs = FakeSettings(), FakeJobs()
    cog = make(settings, jobs)

    with caplog.at_level(logging.INFO, logger="aha.cogs.config"):
        asyncio.run(cog.purge_guild(purge_job()))

    assert settings.calls == [("delete", 11)]
    assert jobs.purged == [11]
    assert "Deleted the data of departed guild 11" in caplog.messages


def test_the_purge_does_nothing_if_the_bot_rejoined() -> None:
    settings, jobs = FakeSettings(), FakeJobs()
    bot = FakeBot(settings)
    bot.guilds[11] = Named(id=11)
    cog = Config(cast("Any", bot), cast("Any", jobs))

    asyncio.run(cog.purge_guild(purge_job()))

    assert settings.calls == []
    assert jobs.purged == []


def test_a_purge_job_without_a_guild_or_a_store_is_ignored() -> None:
    settings, jobs = FakeSettings(), FakeJobs()
    asyncio.run(make(settings, jobs).purge_guild(purge_job(None)))
    asyncio.run(make(settings, None).purge_guild(purge_job()))

    assert settings.calls == []
    assert jobs.purged == []


def test_the_purge_still_clears_jobs_when_settings_are_unavailable() -> None:
    jobs = FakeJobs()

    asyncio.run(make(None, jobs).purge_guild(purge_job()))

    assert jobs.purged == [11]


# ------------------------------------------------------------------------------ setup


def test_setup_adds_the_cog_and_registers_the_purge_handler() -> None:
    bot = FakeBot(None)
    registered: list[tuple[str, Any]] = []
    bot.scheduler = Named(register=lambda kind, handler: registered.append((kind, handler)))

    asyncio.run(config_setup(cast("Any", bot)))

    (cog,) = bot.cogs
    assert registered == [("guild_purge", cog.purge_guild)]


def test_setup_without_a_scheduler_only_adds_the_cog() -> None:
    bot = FakeBot(None)

    asyncio.run(config_setup(cast("Any", bot)))

    assert len(bot.cogs) == 1


def test_the_cog_builds_its_job_store_over_the_bots_database() -> None:
    database = Named()

    cog = Config(cast("Any", FakeBot(None, db=database)))

    assert cast("Any", cog._jobs)._db is database
    assert Config(cast("Any", FakeBot(None)))._jobs is None


def test_utcnow_is_timezone_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()

    now = config_module.utcnow()

    assert now.tzinfo is UTC
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5


# ------------------------------------------------------------------------- German


def german(ctx: StubContext) -> StubContext:
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)
    return ctx


def test_config_speaks_german_to_a_german_client() -> None:
    cog = make(FakeSettings(GuildSettings(11, disabled_commands=frozenset({"roll"}))))
    ctx = german(ctx_for(cast("Any", cog.bot)))
    asyncio.run(cast("Any", cog.config.callback)(cog, as_context(ctx)))
    channel, disabled = "**Log-Kanal:** nicht gesetzt", "**Deaktivierte Befehle:**"
    assert text(ctx) == f"{channel}\n{disabled} `roll`\n**Zusammenfassungen:** aus"

    cog = make(FakeSettings())
    ctx = german(ctx_for(cast("Any", cog.bot)))
    asyncio.run(cast("Any", cog.config.callback)(cog, as_context(ctx)))
    assert text(ctx) == f"{channel}\n{disabled} keine\n**Zusammenfassungen:** aus"

    cog = make(None)
    ctx = german(ctx_for(cast("Any", cog.bot)))
    asyncio.run(cast("Any", cog.config.callback)(cog, as_context(ctx)))
    assert text(ctx) == (
        "Servereinstellungen sind nicht verfügbar: Es ist keine Datenbank konfiguriert."
    )


def test_configure_confirmations_and_refusals_speak_german() -> None:
    cases = [
        ("log-channel", "off", "Log-Kanal entfernt."),
        ("log-channel", "nonsense", "Gib einen Textkanal dieses Servers an, oder `off`."),
        ("disable", "roll", "`roll` ist auf diesem Server jetzt deaktiviert."),
        ("enable", "roll", "`roll` ist auf diesem Server aktiviert."),
        ("disable", "config", "`config` kann nicht deaktiviert werden."),
        ("disable", "nope", "Es gibt keinen Befehl `nope`."),
    ]
    for setting, value, expected in cases:
        cog = make(FakeSettings())
        ctx = german(ctx_for(cast("Any", cog.bot)))
        configure(cog, ctx, setting, value)
        assert text(ctx) == expected

    cog = make(FakeSettings())
    ctx = german(ctx_for(cast("Any", cog.bot), channels={55: FakeChannel(55)}))
    configure(cog, ctx, "log-channel", "55")
    assert text(ctx) == "Log-Kanal gesetzt."


def test_summaries_are_switched_on_and_off_per_server() -> None:
    for answer, expected, enabled in (
        (
            "on",
            "Summaries are on for this server. "
            "Text people ask to summarize is sent to the Claude API.",
            True,
        ),
        ("OFF", "Summaries are off for this server.", False),
    ):
        settings = FakeSettings()
        cog = make(settings)
        ctx = ctx_for(cast("Any", cog.bot))

        configure(cog, ctx, "summaries", f" {answer} ")

        assert settings.calls == [("summaries", 11, enabled, 33)]
        assert text(ctx) == expected


def test_anything_but_on_or_off_is_refused_for_summaries() -> None:
    settings = FakeSettings()
    cog = make(settings)
    ctx = ctx_for(cast("Any", cog.bot))

    configure(cog, ctx, "summaries", "maybe")

    assert settings.calls == []
    assert text(ctx) == "Say `on` or `off`."


def test_the_view_shows_when_summaries_are_on() -> None:
    cog = make(FakeSettings(GuildSettings(11, summaries_enabled=True)))
    ctx = ctx_for(cast("Any", cog.bot))

    asyncio.run(cast("Any", cog.config.callback)(cog, as_context(ctx)))

    assert text(ctx).endswith("**Summaries:** on")


def test_summaries_switch_in_german() -> None:
    cog = make(FakeSettings())
    ctx = german(ctx_for(cast("Any", cog.bot)))
    configure(cog, ctx, "summaries", "on")
    assert text(ctx).startswith("Zusammenfassungen sind für diesen Server eingeschaltet.")

    cog = make(FakeSettings())
    ctx = german(ctx_for(cast("Any", cog.bot)))
    configure(cog, ctx, "summaries", "x")
    assert text(ctx) == "Sag `on` oder `off`."
