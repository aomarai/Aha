"""Parse human durations such as ``90m``, ``1h30m`` or ``2 days``."""

from __future__ import annotations

import re
from datetime import timedelta

__all__ = ["DurationError", "parse_duration"]

_UNITS: dict[str, int] = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "week": 604800,
    "weeks": 604800,
}

_PART = re.compile(r"(\d+)\s*([a-z]+)")
_WHOLE = re.compile(rf"(?:\s*{_PART.pattern}\s*)+", re.IGNORECASE)

#: Longest numeric field accepted, so ``9999999999999999999d`` cannot overflow a timedelta.
_MAX_DIGITS = 7


class DurationError(ValueError):
    """The text is not a duration this bot understands."""


def parse_duration(text: str) -> timedelta:
    """Return the length of time ``text`` names (``"1h 30m"`` is 90 minutes).

    Raises:
        DurationError: for empty text, an unknown unit, a zero length or an absurd number.
    """
    cleaned = text.strip().lower()
    if not _WHOLE.fullmatch(cleaned):
        raise DurationError("Use a duration like `30m`, `2h` or `1d 12h`.")
    total = 0
    for number, unit in _PART.findall(cleaned):
        if unit not in _UNITS or len(number) > _MAX_DIGITS:
            raise DurationError("Use a duration like `30m`, `2h` or `1d 12h`.")
        total += int(number) * _UNITS[unit]
    if total <= 0:
        raise DurationError("The duration must be longer than zero.")
    return timedelta(seconds=total)
