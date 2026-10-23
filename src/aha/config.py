"""Runtime configuration for the Aha bot.

Configuration is read from environment variables so that no secrets are ever
committed. :meth:`Settings.from_env` is pure with respect to the process
environment -- callers may pass an explicit mapping, which keeps it trivial to
unit-test.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["ConfigError", "Settings"]

DEFAULT_LOG_LEVEL = "INFO"

_TOKEN_ENV = "DISCORD_BOT_TOKEN"

# Discord snowflakes are unsigned 64-bit integers; guild IDs are positive.
_MAX_SNOWFLAKE = (1 << 63) - 1


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


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

    token: str
    log_level: str = DEFAULT_LOG_LEVEL
    dev_guild_id: int | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Build :class:`Settings` from environment variables.

        Args:
            env: Mapping to read from. Defaults to :data:`os.environ`.

        Raises:
            ConfigError: if ``DISCORD_BOT_TOKEN`` is missing or empty, if
                ``LOG_LEVEL`` is not a valid level name, or if ``DEV_GUILD_ID``
                is not a positive Discord snowflake.
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

        dev_guild_id = _parse_optional_int(source.get("DEV_GUILD_ID"), name="DEV_GUILD_ID")
        if dev_guild_id is not None and not 0 < dev_guild_id <= _MAX_SNOWFLAKE:
            raise ConfigError(
                "DEV_GUILD_ID must be a positive Discord snowflake "
                f"(1..{_MAX_SNOWFLAKE}), got {dev_guild_id}",
            )

        return cls(
            token=token,
            log_level=log_level,
            dev_guild_id=dev_guild_id,
        )
