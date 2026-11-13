"""Entry point: ``python -m aha`` or the ``aha`` console script.

``aha`` runs the bot. ``aha sync [--guild ID] [--dry-run]`` syncs the slash-command tree to
Discord over REST (no gateway connection) and exits; it is the deploy-time sync that replaces
syncing at every startup (see docs/adr/0001-runtime-architecture.md, section 4).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from aha.bot import AhaBot, SyncResult
from aha.config import MAX_SNOWFLAKE, ConfigError, Settings
from aha.logging_setup import configure_logging, static_fields_from_env

__all__ = ["cli", "main"]

log = logging.getLogger(__name__)

#: Seconds to wait for ``bot.start()`` to unwind after SIGTERM before giving up.
_SHUTDOWN_TIMEOUT = 10.0


async def _serve(bot: AhaBot, token: str) -> None:
    """Run ``bot`` until it stops or SIGTERM asks it to shut down.

    ``Client.run`` only handles ``KeyboardInterrupt``, so a plain SIGTERM
    (``docker stop``, systemd) would kill the process without closing the
    gateway connection. Owning the loop here lets SIGTERM shut the bot down.

    SIGINT is deliberately left to ``asyncio.run``: the first Ctrl+C cancels
    this task (``async with bot`` still closes the client) and a second one
    raises ``KeyboardInterrupt`` even if shutdown hangs.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    # Windows event loops cannot do this; only Ctrl+C is graceful there.
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, stop.set)

    async with bot:
        runner = asyncio.create_task(bot.start(token))
        waiter = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                log.info("Shutdown signal received; closing bot")
                # Cancel start() rather than closing the client under it: it may
                # be mid-login or mid-setup_hook, and closing the HTTP session
                # there would make it fail noisily. Bound the wait so a stuck
                # start() cannot keep the process alive.
                runner.cancel()
                _, pending = await asyncio.wait({runner}, timeout=_SHUTDOWN_TIMEOUT)
                if pending:
                    log.warning("Bot did not stop within %ss; closing anyway", _SHUTDOWN_TIMEOUT)
                elif not runner.cancelled():
                    runner.exception()  # mark retrieved; nothing to report mid-shutdown
            else:
                await runner  # re-raises start() failures such as LoginFailure
        finally:
            waiter.cancel()
            runner.cancel()


def _guild_id(raw: str) -> int:
    """Parse ``--guild``: a positive Discord snowflake."""
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {raw!r}") from None
    if not 0 < value <= MAX_SNOWFLAKE:
        raise argparse.ArgumentTypeError(f"not a positive Discord snowflake: {raw!r}")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aha", description="Run the Aha Discord bot.")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    sync = commands.add_parser(
        "sync",
        help="sync the slash commands to Discord and exit",
        description=(
            "Compare the bot's slash commands with Discord's and sync only if they differ. "
            "Uses REST only (no gateway connection), so it is safe to run as a deploy Job."
        ),
    )
    sync.add_argument(
        "--guild",
        type=_guild_id,
        metavar="ID",
        help="sync to this guild (as a dev server) instead of globally; overrides DEV_GUILD_ID",
    )
    sync.add_argument(
        "--dry-run", action="store_true", help="show what would change without syncing"
    )
    return parser


def _bootstrap() -> Settings | None:
    """Load ``.env``, read the settings and configure logging; ``None`` on a config error."""
    # Load the project's .env explicitly from the current directory. Passing an
    # explicit path avoids python-dotenv's default behaviour of walking up
    # parent directories, which could silently pick up an unrelated .env.
    # Real environment variables always win (load_dotenv does not override).
    load_dotenv(dotenv_path=Path.cwd() / ".env")

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logging.basicConfig(level=logging.ERROR)
        log.exception("%s", exc)
        return None

    configure_logging(
        settings.log_level,
        log_format=settings.log_format,
        secrets=(settings.token,),
        static=static_fields_from_env(),
    )
    return settings


def _run(settings: Settings) -> int:
    """Run the bot until it stops; return the process exit code."""
    # bot.start() (unlike bot.run()) never installs a log handler, so
    # logging_setup stays authoritative.
    exit_code = 0
    try:
        bot = AhaBot(settings)
        asyncio.run(_serve(bot, settings.token))
        # A watchdog-triggered shutdown sets a non-zero code so an
        # "on-failure" supervisor restarts the process.
        exit_code = bot.exit_code
    except KeyboardInterrupt:
        log.info("Interrupted; bot closed")
    except Exception:
        log.exception("Bot stopped with an unexpected error")
        return 1
    return exit_code


async def _sync_once(bot: AhaBot, token: str, *, dry_run: bool) -> list[SyncResult]:
    """Log in over REST (which loads the cogs), sync, and close."""
    async with bot:
        await bot.login(token)
        return await bot.sync_commands(dry_run=dry_run)


def _print_report(results: list[SyncResult], *, dry_run: bool) -> None:
    for result in results:
        if result.diff.unchanged:
            print(f"{result.target}: unchanged")
            continue
        outcome = "would change (dry run)" if dry_run else "synced"
        print(f"{result.target}: {outcome}")
        for line in result.diff.lines():
            print(f"  {line}")


def _sync(settings: Settings, *, guild: int | None, dry_run: bool) -> int:
    """Sync the command tree once; return the process exit code."""
    if guild is not None:
        settings = replace(settings, dev_guild_id=guild)
    # The Job syncs explicitly below; the startup sync must not run a second time.
    bot = AhaBot(settings, sync_on_startup=False)
    try:
        results = asyncio.run(_sync_once(bot, settings.token, dry_run=dry_run))
    except Exception:
        log.exception("Command sync failed")
        return 1
    _print_report(results, dry_run=dry_run)
    return 0


def main(argv: Sequence[str] = ()) -> int:
    """Load configuration, configure logging, and run the bot or a subcommand.

    Returns a process exit code (0 on success, 1 on misconfiguration or an
    unexpected failure such as a rejected token).
    """
    args = _build_parser().parse_args(argv)
    settings = _bootstrap()
    if settings is None:
        return 1
    if args.command == "sync":
        return _sync(settings, guild=args.guild, dry_run=args.dry_run)
    return _run(settings)


def cli() -> int:
    """Console-script entry point: :func:`main` with the real command line."""
    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(cli())
