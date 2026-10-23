"""Entry point: ``python -m aha`` or the ``aha`` console script."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path

from dotenv import load_dotenv

from aha.bot import AhaBot
from aha.config import ConfigError, Settings
from aha.logging_setup import configure_logging

__all__ = ["main"]

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


def main() -> int:
    """Load configuration, configure logging, and run the bot.

    Returns a process exit code (0 on clean shutdown, 1 on misconfiguration or
    an unexpected bot failure such as a rejected token).
    """
    # Load the project's .env explicitly from the current directory. Passing an
    # explicit path avoids python-dotenv's default behaviour of walking up
    # parent directories, which could silently pick up an unrelated .env.
    # Real environment variables always win (load_dotenv does not override).
    load_dotenv(dotenv_path=Path.cwd() / ".env")

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO)
        log.exception("%s", exc)
        return 1

    configure_logging(settings.log_level)

    # bot.start() (unlike bot.run()) never installs a log handler, so
    # logging_setup stays authoritative.
    try:
        bot = AhaBot(settings)
        asyncio.run(_serve(bot, settings.token))
    except KeyboardInterrupt:
        log.info("Interrupted; bot closed")
    except Exception:
        log.exception("Bot stopped with an unexpected error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
