"""Runtime configuration for the Aha bot.

Configuration is read from environment variables so that no secrets are ever
committed. :meth:`Settings.from_env` is pure with respect to the process
environment -- callers may pass an explicit mapping, which keeps it trivial to
unit-test.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = ["LOG_FORMATS", "MAX_SNOWFLAKE", "ConfigError", "Settings"]

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = "text"

#: Health/metrics server defaults: loopback only, so nothing is exposed unless asked.
DEFAULT_HEALTH_BIND = "127.0.0.1"
DEFAULT_HEALTH_PORT = 8080
MAX_PORT = 65535

#: Supported ``LOG_FORMAT`` values.
LOG_FORMATS = frozenset({"text", "json"})

_TOKEN_ENV = "DISCORD_BOT_TOKEN"

# Discord snowflakes are unsigned 64-bit integers; guild IDs are positive.
MAX_SNOWFLAKE = (1 << 63) - 1


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _parse_bool(raw: str | None, *, name: str, default: bool = False) -> bool:
    """Return ``raw`` as a bool (``true/false``, ``1/0``, ``yes/no``, ``on/off``).

    Unset or blank gives ``default``; anything else raises :class:`ConfigError`.
    """
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def _parse_optional_int(raw: str | None, *, name: str) -> int | None:
    """Return ``raw`` as an int, or ``None`` when unset.

    Raises :class:`ConfigError` when the value is present but not a valid
    integer, so misconfiguration fails loudly at startup instead of silently
    disabling behaviour.
    """
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated, immutable application settings."""

    token: str = field(repr=False)
    log_level: str = DEFAULT_LOG_LEVEL
    dev_guild_id: int | None = None
    log_format: str = DEFAULT_LOG_FORMAT
    sync_on_startup: bool = False
    health_bind: str = DEFAULT_HEALTH_BIND
    #: TCP port of the health/metrics server; 0 disables it.
    health_port: int = DEFAULT_HEALTH_PORT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build :class:`Settings` from environment variables.

        Args:
            env: Mapping to read from. Defaults to :data:`os.environ`.

        Raises:
            ConfigError: if ``DISCORD_BOT_TOKEN`` is missing or empty, if ``LOG_LEVEL`` is
                not a valid level name, if ``LOG_FORMAT`` is not ``text`` or ``json``, if
                ``SYNC_ON_STARTUP`` is not a boolean, if ``HEALTH_PORT`` is not a port
                number, or if ``DEV_GUILD_ID`` is not a positive Discord snowflake.
        """
        import os

        source: Mapping[str, str] = os.environ if env is None else env

        token = (source.get(_TOKEN_ENV) or "").strip()
        if not token:
            raise ConfigError(
                f"{_TOKEN_ENV} is not set. Copy .env.example to .env and add "
                "your bot token, then retry.",
            )

        log_level = (source.get("LOG_LEVEL") or DEFAULT_LOG_LEVEL).strip().upper()

        if not logging.getLevelNamesMapping().get(log_level):
            raise ConfigError(f"LOG_LEVEL must be a valid level name, got {log_level!r}")

        log_format = (source.get("LOG_FORMAT") or DEFAULT_LOG_FORMAT).strip().lower()
        if log_format not in LOG_FORMATS:
            raise ConfigError(f"LOG_FORMAT must be 'text' or 'json', got {log_format!r}")

        dev_guild_id = _parse_optional_int(source.get("DEV_GUILD_ID"), name="DEV_GUILD_ID")
        if dev_guild_id is not None and not 0 < dev_guild_id <= MAX_SNOWFLAKE:
            raise ConfigError(
                "DEV_GUILD_ID must be a positive Discord snowflake "
                f"(1..{MAX_SNOWFLAKE}), got {dev_guild_id}",
            )

        sync_on_startup = _parse_bool(source.get("SYNC_ON_STARTUP"), name="SYNC_ON_STARTUP")

        # Strip first: a blank value must not become "", which would bind every interface.
        health_bind = (source.get("HEALTH_BIND") or "").strip() or DEFAULT_HEALTH_BIND
        health_port = _parse_optional_int(source.get("HEALTH_PORT"), name="HEALTH_PORT")
        if health_port is None:
            health_port = DEFAULT_HEALTH_PORT
        if not 0 <= health_port <= MAX_PORT:
            raise ConfigError(
                f"HEALTH_PORT must be 0 (disabled) or 1..{MAX_PORT}, got {health_port}"
            )

        return cls(
            token=token,
            log_level=log_level,
            dev_guild_id=dev_guild_id,
            log_format=log_format,
            sync_on_startup=sync_on_startup,
            health_bind=health_bind,
            health_port=health_port,
        )
