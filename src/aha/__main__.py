"""Entry point: ``python -m aha`` or the ``aha`` console script."""

from __future__ import annotations

import logging
from pathlib import Path

from dotenv import load_dotenv

from aha.bot import AhaBot
from aha.config import ConfigError, Settings
from aha.logging_setup import configure_logging

__all__ = ["main"]

log = logging.getLogger(__name__)


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
        log.error("%s", exc)
        return 1

    configure_logging(settings.log_level)

    # log_handler=None: logging_setup owns the handler configuration.
    AhaBot(settings).run(settings.token, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
