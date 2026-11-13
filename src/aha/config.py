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
from pathlib import Path

__all__ = [
    "ACTIVITY_TYPES",
    "LOG_FORMATS",
    "MAX_SNOWFLAKE",
    "STATUSES",
    "ConfigError",
    "Settings",
]

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = "text"

#: Presence defaults: "Listening to /help", shown as online. ``BOT_ACTIVITY=off`` shows nothing.
DEFAULT_ACTIVITY = "/help"
DEFAULT_ACTIVITY_TYPE = "listening"
DEFAULT_STATUS = "online"
ACTIVITY_TYPES = frozenset({"playing", "listening", "watching", "competing"})
STATUSES = frozenset({"online", "idle", "dnd", "invisible"})
#: Discord rejects an activity name longer than this.
MAX_ACTIVITY_LENGTH = 128

#: Health/metrics server defaults: loopback only, so nothing is exposed unless asked.
DEFAULT_HEALTH_BIND = "127.0.0.1"
DEFAULT_HEALTH_PORT = 8080
MAX_PORT = 65535

#: Supported ``LOG_FORMAT`` values.
LOG_FORMATS = frozenset({"text", "json"})

_TOKEN_ENV = "DISCORD_BOT_TOKEN"
_TOKEN_FILE_ENV = "DISCORD_BOT_TOKEN_FILE"

#: Database pool defaults: a small pool per process, and a statement timeout well under
#: Discord's 3-second interaction acknowledgement window.
DEFAULT_DB_POOL_MAX = 5
MAX_DB_POOL_MAX = 50
DEFAULT_DB_STATEMENT_TIMEOUT = 2.0
MAX_DB_STATEMENT_TIMEOUT = 60.0
_TOKEN_ENCODING = "utf-8"

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


def _read_secret(source: Mapping[str, str], env: str, file_env: str) -> str | None:
    """Return a secret from the variable ``env`` or the file named by ``file_env``, else ``None``.

    A file keeps the secret out of the process environment (``kubectl describe``, crash dumps,
    ``/proc/<pid>/environ``), which is how a Kubernetes Secret should be mounted. Setting both
    is an error rather than a silent precedence rule. The secret itself is never put in an error
    message.
    """
    inline = (source.get(env) or "").strip()
    path = (source.get(file_env) or "").strip()
    if inline and path:
        raise ConfigError(f"Set only one of {env} and {file_env}, not both.")
    if path:
        try:
            secret = Path(path).read_bytes().decode(_TOKEN_ENCODING).strip()
        except (OSError, UnicodeDecodeError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) else "not valid UTF-8"
            raise ConfigError(f"Cannot read {file_env} ({path}): {reason}.") from exc
        if not secret:
            raise ConfigError(f"{file_env} ({path}) is empty.")
        return secret
    return inline or None


def _read_token(source: Mapping[str, str]) -> str:
    """Return the bot token from ``DISCORD_BOT_TOKEN`` or the file ``DISCORD_BOT_TOKEN_FILE``."""
    token = _read_secret(source, _TOKEN_ENV, _TOKEN_FILE_ENV)
    if token is None:
        raise ConfigError(
            f"{_TOKEN_ENV} is not set. Copy .env.example to .env and add "
            "your bot token, then retry.",
        )
    return token


def _read_database_url(source: Mapping[str, str]) -> str | None:
    """Return the PostgreSQL URL from ``DATABASE_URL`` or ``DATABASE_URL_FILE`` (optional).

    The URL carries the password, so it is never echoed back in an error message.
    """
    url = _read_secret(source, "DATABASE_URL", "DATABASE_URL_FILE")
    if url is not None and not url.startswith(("postgres://", "postgresql://")):
        raise ConfigError("DATABASE_URL must start with postgres:// or postgresql://.")
    return url


def _parse_activity(raw: str | None) -> str | None:
    """Return the activity text: the default when blank, ``None`` for ``off``."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_ACTIVITY
    if text.lower() == "off":
        return None
    if len(text) > MAX_ACTIVITY_LENGTH:
        raise ConfigError(
            f"BOT_ACTIVITY must be at most {MAX_ACTIVITY_LENGTH} characters, got {len(text)}"
        )
    return text


def _parse_choice(raw: str | None, *, name: str, choices: frozenset[str], default: str) -> str:
    """Return ``raw`` lower-cased if it is one of ``choices``; the default when blank."""
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value not in choices:
        raise ConfigError(f"{name} must be one of {', '.join(sorted(choices))}, got {value!r}")
    return value


def _parse_seconds(raw: str | None, *, name: str, default: float, maximum: float) -> float:
    """Return ``raw`` as a number of seconds in ``(0, maximum]``; the default when blank."""
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number of seconds, got {raw!r}") from exc
    if not 0 < value <= maximum:
        raise ConfigError(f"{name} must be greater than 0 and at most {maximum:g}, got {value:g}")
    return value


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
    #: Activity text shown under the bot's name; ``None`` shows no activity.
    activity: str | None = DEFAULT_ACTIVITY
    activity_type: str = DEFAULT_ACTIVITY_TYPE
    status: str = DEFAULT_STATUS
    #: PostgreSQL URL; ``None`` runs the bot without a database (stateful features unavailable).
    database_url: str | None = field(default=None, repr=False)
    db_pool_max: int = DEFAULT_DB_POOL_MAX
    #: Seconds a query may run before the server cancels it.
    db_statement_timeout: float = DEFAULT_DB_STATEMENT_TIMEOUT
    health_bind: str = DEFAULT_HEALTH_BIND
    #: TCP port of the health/metrics server; 0 disables it.
    health_port: int = DEFAULT_HEALTH_PORT

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build :class:`Settings` from environment variables.

        Args:
            env: Mapping to read from. Defaults to :data:`os.environ`.

        Raises:
            ConfigError: if neither ``DISCORD_BOT_TOKEN`` nor ``DISCORD_BOT_TOKEN_FILE`` gives
                a token (or both are set), if ``DATABASE_URL`` is set both ways or is not a
                PostgreSQL URL, if the pool size or statement timeout is out of range, or if
                ``LOG_LEVEL``, ``LOG_FORMAT``, ``SYNC_ON_STARTUP``, ``HEALTH_PORT``,
                ``BOT_STATUS``, ``BOT_ACTIVITY_TYPE``, ``BOT_ACTIVITY`` or ``DEV_GUILD_ID``
                is invalid.
        """
        import os

        source: Mapping[str, str] = os.environ if env is None else env

        token = _read_token(source)

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
        activity = _parse_activity(source.get("BOT_ACTIVITY"))
        activity_type = _parse_choice(
            source.get("BOT_ACTIVITY_TYPE"),
            name="BOT_ACTIVITY_TYPE",
            choices=ACTIVITY_TYPES,
            default=DEFAULT_ACTIVITY_TYPE,
        )
        status = _parse_choice(
            source.get("BOT_STATUS"), name="BOT_STATUS", choices=STATUSES, default=DEFAULT_STATUS
        )

        database_url = _read_database_url(source)
        db_pool_max = _parse_optional_int(source.get("DB_POOL_MAX"), name="DB_POOL_MAX")
        if db_pool_max is None:
            db_pool_max = DEFAULT_DB_POOL_MAX
        if not 1 <= db_pool_max <= MAX_DB_POOL_MAX:
            raise ConfigError(f"DB_POOL_MAX must be 1..{MAX_DB_POOL_MAX}, got {db_pool_max}")
        db_statement_timeout = _parse_seconds(
            source.get("DB_STATEMENT_TIMEOUT"),
            name="DB_STATEMENT_TIMEOUT",
            default=DEFAULT_DB_STATEMENT_TIMEOUT,
            maximum=MAX_DB_STATEMENT_TIMEOUT,
        )

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
            activity=activity,
            activity_type=activity_type,
            status=status,
            database_url=database_url,
            db_pool_max=db_pool_max,
            db_statement_timeout=db_statement_timeout,
            health_bind=health_bind,
            health_port=health_port,
        )
