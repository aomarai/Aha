"""Central logging configuration for the Aha bot."""

from __future__ import annotations

from logging.config import dictConfig

__all__ = ["configure_logging"]

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(level: str = "INFO") -> None:
    """Configure root logging and quiet down noisy third-party loggers.

    discord.py adds its own handler by default; the bot is run with
    ``log_handler=None`` so this configuration is authoritative.
    """
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {"format": _LOG_FORMAT, "datefmt": "%Y-%m-%d %H:%M:%S"},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {
                # aiohttp logs every request at INFO which is far too chatty.
                "discord": {"level": level},
                "aiohttp": {"level": "WARNING"},
            },
        },
    )
