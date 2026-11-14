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

#: Fraction of the daily gateway sessions below which the bot waits for the reset (0 disables).
DEFAULT_SESSION_START_FLOOR = 0.1

#: ``/summarize`` defaults: a Haiku-class model, a small daily quota per user, bounded input/output.
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"
DEFAULT_SUMMARIZE_DAILY_QUOTA = 10
MAX_SUMMARIZE_DAILY_QUOTA = 1000
DEFAULT_SUMMARIZE_MAX_CHARS = 4000
MIN_SUMMARIZE_MAX_CHARS = 100
MAX_SUMMARIZE_MAX_CHARS = 20_000

#: Most shards one deployment may declare (only catches typos; Discord has its own ceiling).
MAX_SHARD_COUNT = 10_000

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


def _parse_bounded_int(raw: str | None, *, name: str, default: int, low: int, high: int) -> int:
    """Parse an integer in ``low..high``; unset or blank gives ``default``."""
    value = _parse_optional_int(raw, name=name)
    if value is None:
        return default
    if not low <= value <= high:
        raise ConfigError(f"{name} must be {low}..{high}, got {value}")
    return value


def _parse_fraction(raw: str | None, *, name: str, default: float) -> float:
    """Parse a fraction in ``0 <= x < 1``; unset or blank gives ``default``."""
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not 0 <= value < 1:  # also rejects nan
        raise ConfigError(f"{name} must be at least 0 and below 1, got {raw!r}")
    return value


def _parse_shard_ids(raw: str) -> tuple[int, ...]:
    """Parse ``SHARD_IDS`` such as ``0,1,2`` into a tuple of ints."""
    parts = [part.strip() for part in raw.split(",")]
    try:
        return tuple(int(part) for part in parts)
    except ValueError as exc:
        raise ConfigError(f"SHARD_IDS must be comma-separated integers, got {raw!r}") from exc


def _parse_shards(source: Mapping[str, str]) -> tuple[int | None, tuple[int, ...] | None]:
    """Resolve ``(shard_count, shard_ids)`` from the environment, or raise :class:`ConfigError`.

    Three modes: nothing set (Discord picks the count, one process owns every shard: single-pod
    use only); ``SHARD_COUNT`` with explicit ``SHARD_IDS``; or ``SHARD_COUNT`` with
    ``SHARDS_PER_POD`` and ``POD_INDEX`` (a StatefulSet ordinal), which derives this pod's block.
    discord.py requires ``shard_count`` whenever ids are given.
    """
    count = _parse_optional_int(source.get("SHARD_COUNT"), name="SHARD_COUNT")
    raw_ids = (source.get("SHARD_IDS") or "").strip()
    per_pod = _parse_optional_int(source.get("SHARDS_PER_POD"), name="SHARDS_PER_POD")
    index = _parse_optional_int(source.get("POD_INDEX"), name="POD_INDEX")
    if count is not None and not 1 <= count <= MAX_SHARD_COUNT:
        raise ConfigError(f"SHARD_COUNT must be 1..{MAX_SHARD_COUNT}, got {count}")
    if raw_ids and per_pod is not None:
        raise ConfigError("Set either SHARD_IDS or SHARDS_PER_POD, not both")
    if raw_ids:
        if count is None:
            raise ConfigError("SHARD_COUNT is required when SHARD_IDS is set")
        ids = _parse_shard_ids(raw_ids)
        if len(set(ids)) != len(ids):
            raise ConfigError(f"SHARD_IDS must not repeat a shard, got {raw_ids!r}")
        outside = [shard for shard in ids if not 0 <= shard < count]
        if outside:
            raise ConfigError(f"SHARD_IDS {outside} are outside 0..{count - 1} (SHARD_COUNT)")
        return count, ids
    if per_pod is None:
        if index is not None:
            raise ConfigError("POD_INDEX needs SHARDS_PER_POD")
        return count, None
    if count is None:
        raise ConfigError("SHARD_COUNT is required when SHARDS_PER_POD is set")
    if per_pod < 1:
        raise ConfigError(f"SHARDS_PER_POD must be at least 1, got {per_pod}")
    if index is None or index < 0:
        raise ConfigError("SHARDS_PER_POD needs POD_INDEX (the pod ordinal, 0 or more)")
    block = tuple(range(index * per_pod, min((index + 1) * per_pod, count)))
    if not block:
        raise ConfigError(
            f"POD_INDEX {index} owns no shards: SHARD_COUNT {count} with {per_pod} per pod "
            f"covers pods 0..{(count - 1) // per_pod}"
        )
    return count, block


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
    #: Total shards Discord splits the bot into; ``None`` lets Discord recommend (single pod only).
    shard_count: int | None = None
    #: The shards this process owns; ``None`` means all of them. Needs ``shard_count``.
    shard_ids: tuple[int, ...] | None = None
    #: Refuse to identify when fewer than this fraction of the daily sessions remain (0 disables).
    session_start_floor: float = DEFAULT_SESSION_START_FLOOR
    #: Anthropic API key for ``/summarize``; ``None`` leaves the command unavailable.
    anthropic_api_key: str | None = field(default=None, repr=False)
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    #: Summaries one user may request per UTC day.
    summarize_daily_quota: int = DEFAULT_SUMMARIZE_DAILY_QUOTA
    #: Longest text (characters) that is sent to the model.
    summarize_max_chars: int = DEFAULT_SUMMARIZE_MAX_CHARS

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

        shard_count, shard_ids = _parse_shards(source)
        anthropic_api_key = _read_secret(source, "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_FILE")
        anthropic_model = (source.get("ANTHROPIC_MODEL") or "").strip() or DEFAULT_ANTHROPIC_MODEL
        summarize_daily_quota = _parse_bounded_int(
            source.get("SUMMARIZE_DAILY_QUOTA"),
            name="SUMMARIZE_DAILY_QUOTA",
            default=DEFAULT_SUMMARIZE_DAILY_QUOTA,
            low=1,
            high=MAX_SUMMARIZE_DAILY_QUOTA,
        )
        summarize_max_chars = _parse_bounded_int(
            source.get("SUMMARIZE_MAX_CHARS"),
            name="SUMMARIZE_MAX_CHARS",
            default=DEFAULT_SUMMARIZE_MAX_CHARS,
            low=MIN_SUMMARIZE_MAX_CHARS,
            high=MAX_SUMMARIZE_MAX_CHARS,
        )
        session_start_floor = _parse_fraction(
            source.get("SESSION_START_FLOOR"),
            name="SESSION_START_FLOOR",
            default=DEFAULT_SESSION_START_FLOOR,
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
            shard_count=shard_count,
            shard_ids=shard_ids,
            session_start_floor=session_start_floor,
            anthropic_api_key=anthropic_api_key,
            anthropic_model=anthropic_model,
            summarize_daily_quota=summarize_daily_quota,
            summarize_max_chars=summarize_max_chars,
        )
