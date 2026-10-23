"""Entry point: ``python -m aha`` or the ``aha`` console script."""

from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from dotenv import load_dotenv

from aha.bot import AhaBot
from aha.config import ConfigError, Settings
from aha.logging_setup import configure_logging

__all__ = ["main"]

log = logging.getLogger(__name__)


async def _serve(bot: AhaBot, token: str) -> None:
    """Run ``bot`` until it stops or SIGINT/SIGTERM asks it to shut down.

    ``Client.run`` only handles ``KeyboardInterrupt``, so a plain SIGTERM
    (``docker stop``, systemd) would kill the process without closing the
    gateway connection. Owning the loop here lets signals trigger ``close()``.
    """
    try:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                # Windows event loops cannot do this; Ctrl+C still arrives as
                # KeyboardInterrupt, which cancels this task and exits the context.
                break

        async with bot:
            runner = asyncio.create_task(bot.start(token))
            waiter = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                log.info("Shutdown signal received; closing bot")
            else:
                waiter.cancel()
            await bot.close()
            await runner  # re-raises start() failures such as LoginFailure
    except RuntimeError as excp:
        log.exception("Unexpected error during bot startup: %s", excp)
        raise


def main() -> int:
    """Load configuration, configure logging, and run the bot.

    Returns a process exit code (0 on clean shutdown, 1 on misconfiguration).
    """
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
        return 1

    configure_logging(settings.log_level)

    # bot.start() (unlike bot.run()) never installs a log handler, so
    # logging_setup stays authoritative.
    try:
        asyncio.run(_serve(AhaBot(settings), settings.token))
    except KeyboardInterrupt:
        log.info("Interrupted; bot closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
