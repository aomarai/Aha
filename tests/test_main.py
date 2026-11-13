"""Tests for the entry point (:mod:`aha.__main__`)."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from typing import Any, cast

import pytest

from aha import __main__ as aha_main
from aha import __version__
from aha.bot import AhaBot, SyncResult
from aha.cmdsync import CommandDiff
from aha.dbmigrate import Migration, MigrationStatus

#: Environment variables :meth:`Settings.from_env` reads, besides the token.
_SETTINGS_ENV_VARS = (
    "LOG_LEVEL",
    "LOG_FORMAT",
    "DEV_GUILD_ID",
    "DATABASE_URL",
    "DATABASE_URL_FILE",
    "SYNC_ON_STARTUP",
    "HOSTNAME",
    "AHA_VERSION",
    "AHA_COMMIT",
)


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

    def fake_configure(level: str, **kwargs: Any) -> None:
        events.append(level)

    monkeypatch.setattr(aha_main, "configure_logging", fake_configure)
    monkeypatch.setattr(aha_main, "_serve", fake_serve)

    assert aha_main.main() == 0
    assert events == ["DEBUG", "serve"]


def test_main_passes_the_format_token_and_static_fields_to_logging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("LOG_FORMAT", "json")
    monkeypatch.setenv("HOSTNAME", "aha-0")
    monkeypatch.setenv("AHA_VERSION", "1.2.3")
    captured: dict[str, Any] = {}

    async def fake_serve(bot: AhaBot, token: str) -> None:
        return None

    def fake_configure(level: str, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(aha_main, "configure_logging", fake_configure)
    monkeypatch.setattr(aha_main, "_serve", fake_serve)

    assert aha_main.main() == 0
    assert captured == {
        "log_format": "json",
        "secrets": ("dummy-token",),
        "static": {"pod": "aha-0", "version": "1.2.3"},
    }


def test_main_returns_the_bot_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")

    async def fake_serve(bot: AhaBot, token: str) -> None:
        bot.exit_code = 7

    monkeypatch.setattr(aha_main, "configure_logging", lambda level, **kwargs: None)
    monkeypatch.setattr(aha_main, "_serve", fake_serve)

    assert aha_main.main() == 7


def test_main_exits_cleanly_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")

    async def fake_serve(bot: AhaBot, token: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(aha_main, "configure_logging", lambda level, **kwargs: None)
    monkeypatch.setattr(aha_main, "_serve", fake_serve)

    with caplog.at_level(logging.INFO, logger="aha.__main__"):
        assert aha_main.main() == 0
    assert "Interrupted; bot closed" in [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("bad token")])
def test_main_returns_one_and_logs_when_the_bot_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")

    async def fake_serve(bot: AhaBot, token: str) -> None:
        raise error

    monkeypatch.setattr(aha_main, "configure_logging", lambda level, **kwargs: None)
    monkeypatch.setattr(aha_main, "_serve", fake_serve)

    with caplog.at_level(logging.INFO, logger="aha.__main__"):
        assert aha_main.main() == 1

    (record,) = caplog.records
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Bot stopped with an unexpected error"
    assert record.exc_info is not None
    assert record.exc_info[1] is error


class _FakeBot:
    """Stands in for AhaBot: ``start`` blocks until closed, like the gateway."""

    def __init__(
        self, *, fail_start: BaseException | None = None, ignore_cancel: bool = False
    ) -> None:
        self.events: list[str] = []
        self._closed = asyncio.Event()
        self._fail_start = fail_start
        self._ignore_cancel = ignore_cancel

    async def __aenter__(self) -> _FakeBot:
        self.events.append("enter")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()  # like Client.__aexit__

    async def start(self, token: str) -> None:
        self.events.append(f"start:{token}")
        if self._fail_start is not None:
            raise self._fail_start
        try:
            await self._closed.wait()
        except asyncio.CancelledError:
            self.events.append("cancelled")
            if self._ignore_cancel:
                await asyncio.sleep(60)  # a start() stuck in login / setup_hook
            raise

    async def close(self) -> None:
        self.events.append("close")
        self._closed.set()


def _patch_signals(
    monkeypatch: pytest.MonkeyPatch, *, unsupported: bool = False
) -> dict[signal.Signals, Any]:
    """Intercept ``add_signal_handler`` on the *running* loop instance.

    Patching the instance works for every loop implementation; patching
    ``AbstractEventLoop`` would miss the override on Unix selector loops.
    """
    handlers: dict[signal.Signals, Any] = {}

    def add_signal_handler(sig: signal.Signals, callback: Any) -> None:
        if unsupported:
            raise NotImplementedError
        handlers[sig] = callback

    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", add_signal_handler)
    return handlers


def test_serve_cancels_start_and_closes_when_sigterm_arrives(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _FakeBot()

    async def scenario() -> None:
        handlers = _patch_signals(monkeypatch)
        serving = asyncio.create_task(aha_main._serve(cast("AhaBot", bot), "tok"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        # SIGINT stays with asyncio.run so a second Ctrl+C can break a hung shutdown.
        assert set(handlers) == {signal.SIGTERM}
        handlers[signal.SIGTERM]()  # what the event loop does on SIGTERM
        await serving

    with caplog.at_level(logging.INFO, logger="aha.__main__"):
        asyncio.run(scenario())

    assert bot.events == ["enter", "start:tok", "cancelled", "close"]
    assert [r.getMessage() for r in caplog.records] == ["Shutdown signal received; closing bot"]


def test_serve_gives_up_waiting_on_a_stuck_start(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(aha_main, "_SHUTDOWN_TIMEOUT", 0.01)
    bot = _FakeBot(ignore_cancel=True)

    async def scenario() -> None:
        handlers = _patch_signals(monkeypatch)
        serving = asyncio.create_task(aha_main._serve(cast("AhaBot", bot), "tok"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        handlers[signal.SIGTERM]()
        await serving

    with caplog.at_level(logging.INFO, logger="aha.__main__"):
        asyncio.run(scenario())

    assert bot.events == ["enter", "start:tok", "cancelled", "close"]
    assert [r.getMessage() for r in caplog.records] == [
        "Shutdown signal received; closing bot",
        "Bot did not stop within 0.01s; closing anyway",
    ]


def test_serve_closes_the_bot_when_the_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # asyncio.run's SIGINT handler cancels the main task; the client must still close.
    bot = _FakeBot()

    async def scenario() -> None:
        _patch_signals(monkeypatch)
        serving = asyncio.create_task(aha_main._serve(cast("AhaBot", bot), "tok"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        serving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serving

    asyncio.run(scenario())

    assert bot.events == ["enter", "start:tok", "close", "cancelled"]


def test_serve_propagates_start_failures_without_a_shutdown_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bot = _FakeBot(fail_start=RuntimeError("login failed"))

    async def scenario() -> None:
        _patch_signals(monkeypatch)
        await aha_main._serve(cast("AhaBot", bot), "tok")

    serving = scenario()
    with (
        caplog.at_level(logging.INFO, logger="aha.__main__"),
        pytest.raises(RuntimeError, match="login failed"),
    ):
        asyncio.run(serving)

    assert bot.events == ["enter", "start:tok", "close"]
    assert not caplog.records


def test_serve_ignores_errors_raised_by_start_while_shutting_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Failing(_FakeBot):
        async def start(self, token: str) -> None:
            try:
                await self._closed.wait()
            except asyncio.CancelledError:
                raise RuntimeError("teardown failed") from None

    bot = _Failing()

    async def scenario() -> None:
        handlers = _patch_signals(monkeypatch)
        serving = asyncio.create_task(aha_main._serve(cast("AhaBot", bot), "tok"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        handlers[signal.SIGTERM]()
        await serving

    with caplog.at_level(logging.INFO, logger="aha.__main__"):
        asyncio.run(scenario())

    assert bot.events == ["enter", "close"]
    assert [r.getMessage() for r in caplog.records] == ["Shutdown signal received; closing bot"]


def test_serve_tolerates_loops_without_signal_support(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _FakeBot(fail_start=RuntimeError("stop here"))

    async def scenario() -> None:
        _patch_signals(monkeypatch, unsupported=True)
        await aha_main._serve(cast("AhaBot", bot), "tok")

    serving = scenario()
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(serving)

    assert bot.events == ["enter", "start:tok", "close"]


def _fake_sync_once(
    monkeypatch: pytest.MonkeyPatch,
    results: list[SyncResult] | Exception,
    seen: dict[str, Any] | None = None,
) -> None:
    async def fake(bot: AhaBot, token: str, *, dry_run: bool) -> list[SyncResult]:
        if seen is not None:
            seen.update(bot=bot, token=token, dry_run=dry_run)
        if isinstance(results, Exception):
            raise results
        return results

    monkeypatch.setattr(aha_main, "_sync_once", fake)
    monkeypatch.setattr(aha_main, "configure_logging", lambda level, **kwargs: None)


def test_sync_subcommand_syncs_and_prints_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    seen: dict[str, Any] = {}
    diff = CommandDiff(added=("ping",), removed=("old",), changed=("help",))
    _fake_sync_once(monkeypatch, [SyncResult("global", diff, synced=True)], seen)

    assert aha_main.main(["sync"]) == 0

    assert seen["token"] == "dummy-token"
    assert seen["dry_run"] is False
    assert seen["bot"].sync_on_startup is False  # the Job syncs explicitly, once
    assert seen["bot"].health_server_enabled is False  # a short REST-only Job must not listen
    assert seen["bot"].settings.dev_guild_id is None
    assert capsys.readouterr().out == "global: synced\n  + ping\n  - old\n  ~ help\n"


def test_sync_dry_run_is_reported_as_would_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    seen: dict[str, Any] = {}
    results = [
        SyncResult("global", CommandDiff(added=("ping",)), synced=False),
        SyncResult("dev guild 5", CommandDiff(), synced=False),
    ]
    _fake_sync_once(monkeypatch, results, seen)

    assert aha_main.main(["sync", "--dry-run"]) == 0

    assert seen["dry_run"] is True
    assert capsys.readouterr().out == (
        "global: would change (dry run)\n  + ping\ndev guild 5: unchanged\n"
    )


def test_sync_guild_option_overrides_dev_guild_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("DEV_GUILD_ID", "111")
    seen: dict[str, Any] = {}
    _fake_sync_once(monkeypatch, [], seen)

    assert aha_main.main(["sync", "--guild", "222"]) == 0

    assert seen["bot"].settings.dev_guild_id == 222
    assert seen["bot"].sync_on_startup is False


def test_sync_failure_is_logged_and_returns_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    _fake_sync_once(monkeypatch, RuntimeError("rejected token"))

    with caplog.at_level(logging.ERROR, logger="aha.__main__"):
        assert aha_main.main(["sync"]) == 1

    assert [r.getMessage() for r in caplog.records] == ["Command sync failed"]


def test_sync_without_a_token_returns_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _isolate_environment(monkeypatch, tmp_path)

    assert aha_main.main(["sync"]) == 1


@pytest.mark.parametrize("raw", ["abc", "0", "-5", str(1 << 63)])
def test_sync_rejects_an_invalid_guild_id(
    raw: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_environment(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as caught:
        aha_main.main(["sync", "--guild", raw])

    assert caught.value.code == 2


def test_guild_id_accepts_a_snowflake() -> None:
    assert aha_main._guild_id("123456789012345678") == 123456789012345678


def test_an_unknown_subcommand_is_rejected() -> None:
    with pytest.raises(SystemExit) as caught:
        aha_main.main(["bogus"])

    assert caught.value.code == 2


def test_cli_passes_the_command_line_to_main(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[Any] = []

    def fake_main(argv: Any) -> int:
        received.append(list(argv))
        return 3

    monkeypatch.setattr(aha_main, "main", fake_main)
    monkeypatch.setattr("sys.argv", ["aha", "sync", "--dry-run"])

    assert aha_main.cli() == 3
    assert received == [["sync", "--dry-run"]]


class _FakeJobBot:
    """Stands in for ``AhaBot`` in ``_sync_once``: an async context manager with login/sync."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def __aenter__(self) -> _FakeJobBot:
        self.events.append("enter")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.events.append("exit")

    async def login(self, token: str) -> None:
        self.events.append(f"login:{token}")

    async def sync_commands(self, *, dry_run: bool) -> list[SyncResult]:
        self.events.append(f"sync:{dry_run}")
        return [SyncResult("global", CommandDiff(), synced=False)]


def test_sync_once_logs_in_over_rest_syncs_and_closes() -> None:
    bot = _FakeJobBot()

    results = asyncio.run(aha_main._sync_once(cast("AhaBot", bot), "tok", dry_run=True))

    assert bot.events == ["enter", "login:tok", "sync:True", "exit"]
    assert [r.target for r in results] == ["global"]


_MAIN_HELP = (
    "usage: aha [-h] [--version] COMMAND ...\n\nRun the Aha Discord bot.\n\n"
    "positional arguments:\n"
    "  COMMAND\n    sync      sync the slash commands to Discord and exit\n"
    "    migrate   apply pending database migrations and exit\n\noptions:\n"
    "  -h, --help  show this help message and exit\n"
    "  --version   show program's version number and exit\n"
)

_SYNC_HELP = (
    "usage: aha sync [-h] [--guild ID] [--dry-run]\n\n"
    "Compare the bot's slash commands with Discord's and sync only if they differ. "
    "Uses REST only (no\ngateway connection), so it is safe to run as a deploy Job.\n\n"
    "options:\n  -h, --help  show this help message and exit\n"
    "  --guild ID  sync to this guild (as a dev server) instead of globally; "
    "overrides DEV_GUILD_ID\n"
    "  --dry-run   show what would change without syncing\n"
)


_MIGRATE_HELP = (
    "usage: aha migrate [-h] [--status]\n\n"
    "Apply the SQL migrations the database is missing, in order, under an advisory lock (so two "
    "runs\ncannot overlap), then exit. Run it as a deploy Job before the new revision rolls out; "
    "the bot\nitself never migrates at startup.\n\n"
    "options:\n  -h, --help  show this help message and exit\n"
    "  --status    show what is applied and pending; change nothing\n"
)


def test_the_help_text_documents_the_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    monkeypatch.setenv("NO_COLOR", "1")
    parser = aha_main._build_parser()
    choices = cast("Any", parser)._subparsers._group_actions[0].choices

    assert parser.format_help() == _MAIN_HELP
    assert choices["sync"].format_help() == _SYNC_HELP
    assert choices["migrate"].format_help() == _MIGRATE_HELP


def test_a_bad_guild_id_explains_what_is_wrong(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_environment(monkeypatch, tmp_path)

    for raw, message in (
        ("abc", "argument --guild: not an integer: 'abc'"),
        ("0", "argument --guild: not a positive Discord snowflake: '0'"),
    ):
        with pytest.raises(SystemExit):
            aha_main.main(["sync", "--guild", raw])
        assert message in capsys.readouterr().err


def test_guild_id_bounds_are_inclusive() -> None:
    assert aha_main._guild_id("1") == 1
    assert aha_main._guild_id(str((1 << 63) - 1)) == (1 << 63) - 1


def test_the_report_continues_past_an_unchanged_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    results = [
        SyncResult("dev guild 5", CommandDiff(), synced=False),
        SyncResult("global", CommandDiff(removed=("old",)), synced=True),
    ]

    aha_main._print_report(results, dry_run=False)

    assert capsys.readouterr().out == "dev guild 5: unchanged\nglobal: synced\n  - old\n"


def test_version_flag_prints_the_package_version_and_exits(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        aha_main.main(["--version"])

    assert caught.value.code == 0
    assert capsys.readouterr().out == f"aha {__version__}\n"


# ------------------------------------------------------------------------------- aha migrate


def _fake_migrate_once(
    monkeypatch: pytest.MonkeyPatch, result: Any, seen: dict[str, Any] | None = None
) -> None:
    async def fake(url: str, *, status_only: bool) -> Any:
        if seen is not None:
            seen.update(url=url, status_only=status_only)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(aha_main, "_migrate_once", fake)
    monkeypatch.setattr(aha_main, "configure_logging", lambda level, **kwargs: None)


def _status(applied: tuple[int, ...], pending: tuple[Migration, ...]) -> MigrationStatus:
    return MigrationStatus(applied, pending)


def test_migrate_reports_what_it_applied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pw@db/aha")
    seen: dict[str, Any] = {}
    done = [Migration(2, "add_things", "x"), Migration(3, "more", "y")]
    _fake_migrate_once(monkeypatch, (done, _status((1, 2, 3), ())), seen)

    assert aha_main.main(["migrate"]) == 0

    assert seen == {"url": "postgresql://u:pw@db/aha", "status_only": False}
    assert capsys.readouterr().out == "applied 0002_add_things\napplied 0003_more\n"


def test_migrate_says_when_there_is_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/aha")
    _fake_migrate_once(monkeypatch, ([], _status((1,), ())))

    assert aha_main.main(["migrate"]) == 0

    assert capsys.readouterr().out == "up to date\n"


def test_migrate_status_lists_applied_and_pending_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/aha")
    seen: dict[str, Any] = {}
    pending = (Migration(2, "a", "x"), Migration(3, "b", "y"))
    _fake_migrate_once(monkeypatch, ([], _status((1,), pending)), seen)

    assert aha_main.main(["migrate", "--status"]) == 0

    assert seen["status_only"] is True
    assert capsys.readouterr().out == "applied: 0001\npending: 0002, 0003\n"


def test_migrate_status_of_an_empty_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/aha")
    _fake_migrate_once(monkeypatch, ([], _status((), (Migration(1, "baseline", "x"),))))

    assert aha_main.main(["migrate", "--status"]) == 0

    assert capsys.readouterr().out == "applied: none\npending: 0001\n"


def test_migrate_without_a_database_url_fails_clearly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setattr(aha_main, "configure_logging", lambda level, **kwargs: None)

    with caplog.at_level(logging.ERROR, logger="aha.__main__"):
        assert aha_main.main(["migrate"]) == 1

    assert [r.getMessage() for r in caplog.records] == [
        "DATABASE_URL (or DATABASE_URL_FILE) is not set; there is nothing to migrate."
    ]


def test_a_failed_migration_returns_one_without_logging_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _isolate_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "dummy-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:hunter2@db/aha")
    _fake_migrate_once(monkeypatch, RuntimeError("syntax error in 0002"))

    with caplog.at_level(logging.ERROR, logger="aha.__main__"):
        assert aha_main.main(["migrate"]) == 1

    assert [r.getMessage() for r in caplog.records] == ["Migration failed"]
    assert "hunter2" not in caplog.text.replace("hunter2@", "") or "hunter2" not in "".join(
        r.getMessage() for r in caplog.records
    )


def test_the_real_migrate_once_applies_and_closes_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Conn:
        async def close(self) -> None:
            events.append("close")

    async def fake_connect(url: str) -> Conn:
        events.append(f"connect {url}")
        return Conn()

    async def fake_migrate(conn: Any, migrations: Any) -> list[Migration]:
        events.append("migrate")
        return [Migration(1, "baseline", "x")]

    async def fake_status(conn: Any, migrations: Any) -> MigrationStatus:
        events.append("status")
        return _status((1,), ())

    monkeypatch.setattr(aha_main, "connect_once", fake_connect)
    monkeypatch.setattr(aha_main, "migrate", fake_migrate)
    monkeypatch.setattr(aha_main, "status", fake_status)

    applied, current = asyncio.run(aha_main._migrate_once("postgresql://db/aha", status_only=False))

    assert ([m.version for m in applied], current.applied) == ([1], (1,))
    assert events == ["connect postgresql://db/aha", "migrate", "status", "close"]


def test_the_real_status_only_path_never_migrates_and_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Conn:
        async def close(self) -> None:
            events.append("close")

    async def fake_connect(url: str) -> Conn:
        return Conn()

    async def fake_migrate(conn: Any, migrations: Any) -> list[Migration]:
        raise AssertionError("must not migrate")

    async def fake_status(conn: Any, migrations: Any) -> MigrationStatus:
        events.append("status")
        raise RuntimeError("lost connection")

    monkeypatch.setattr(aha_main, "connect_once", fake_connect)
    monkeypatch.setattr(aha_main, "migrate", fake_migrate)
    monkeypatch.setattr(aha_main, "status", fake_status)

    with pytest.raises(RuntimeError, match="lost connection"):
        asyncio.run(aha_main._migrate_once("postgresql://db/aha", status_only=True))

    assert events == ["status", "close"]  # closed even though status failed
