"""Central logging configuration for the Aha bot.

Two output formats share one set of safeguards:

* ``text`` -- the human-readable line used for local development.
* ``json`` -- one JSON object per line for log stores such as Loki: a fixed set of keys
  (``ts``, ``level``, ``logger``, ``msg``, plus ``exc`` for a traceback), the static
  ``pod``/``version``/``commit`` fields, the per-invocation context from
  :mod:`aha.logctx` (``shard_id``, ``guild_id``, ``channel_id``, ``user_id``,
  ``interaction_id``, ``command``) and any ``extra=`` fields the caller passed.

Both formatters replace every configured secret (the bot token) with ``***`` in the final
output, so a token that reaches a log message or a traceback is never written out.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from logging.config import dictConfig
from typing import Any, override

from aha import logctx

__all__ = [
    "JsonFormatter",
    "TextFormatter",
    "configure_logging",
    "static_fields_from_env",
]

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_MASK = "***"

#: Environment variables copied into every JSON record, keyed by their output name.
_STATIC_ENV = {"pod": "HOSTNAME", "version": "AHA_VERSION", "commit": "AHA_COMMIT"}

#: Attributes every ``LogRecord`` has; anything else was supplied through ``extra=``.
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
}


def static_fields_from_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the ``pod``/``version``/``commit`` fields that are set in ``env``."""
    source = os.environ if env is None else env
    return {key: source[name] for key, name in _STATIC_ENV.items() if source.get(name)}


class _MaskingMixin(logging.Formatter):
    """Formatter that replaces secrets in its rendered output."""

    def __init__(self, secrets: Iterable[str] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Longest first, so a secret that contains another is masked whole.
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    def _mask(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, _MASK)
        return text


class TextFormatter(_MaskingMixin):
    """The human-readable single-line format, with secrets masked."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__(secrets, fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)

    @override
    def format(self, record: logging.LogRecord) -> str:
        return self._mask(super().format(record))


class JsonFormatter(_MaskingMixin):
    """Render each record as one JSON object, with context fields and secrets masked."""

    def __init__(
        self, secrets: Iterable[str] = (), static: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(secrets)
        self._static = dict(static or {})

    @override
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **self._static,
            **logctx.current(),
            **{key: value for key, value in vars(record).items() if key not in _RESERVED},
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        return self._mask(json.dumps(payload, default=str, ensure_ascii=False))


def configure_logging(
    level: str = "INFO",
    *,
    log_format: str = "text",
    secrets: Iterable[str] = (),
    static: Mapping[str, str] | None = None,
) -> None:
    """Configure root logging and quiet down noisy third-party loggers.

    discord.py adds its own handler by default; the bot is run with
    ``log_handler=None`` so this configuration is authoritative.

    Args:
        level: Root log level name.
        log_format: ``"text"`` or ``"json"``.
        secrets: Strings (the bot token) that must never appear in the output.
        static: Fields added to every JSON record (see :func:`static_fields_from_env`).
    """
    formatter: dict[str, Any] = (
        {"()": JsonFormatter, "secrets": tuple(secrets), "static": dict(static or {})}
        if log_format == "json"
        else {"()": TextFormatter, "secrets": tuple(secrets)}
    )
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"standard": formatter},
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
