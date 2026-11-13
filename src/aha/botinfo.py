"""Facts about the running bot that commands may show: build identity and latency."""

from __future__ import annotations

import math
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass

import discord

from aha import __version__

__all__ = ["UNKNOWN", "BuildInfo", "build_info", "format_latency"]

#: Shown for a build that did not record where it came from (a plain local run).
UNKNOWN = "unknown"

#: Characters of a commit SHA to show, as ``git log --abbrev-commit`` does.
_SHORT_SHA = 7


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """What is running: the package version, the commit it was built from, and its runtime."""

    version: str
    commit: str
    python: str
    discord_py: str

    @property
    def short_commit(self) -> str:
        """The commit abbreviated for display (``unknown`` stays as it is)."""
        return self.commit if self.commit == UNKNOWN else self.commit[:_SHORT_SHA]


def build_info(env: Mapping[str, str] | None = None) -> BuildInfo:
    """Read the build identity from ``AHA_VERSION`` / ``AHA_COMMIT`` (set by the image build).

    The version falls back to the package's own; the commit falls back to ``unknown``. Blank
    values count as unset.
    """
    source = os.environ if env is None else env
    return BuildInfo(
        version=(source.get("AHA_VERSION") or "").strip() or __version__,
        commit=(source.get("AHA_COMMIT") or "").strip() or UNKNOWN,
        python=platform.python_version(),
        discord_py=discord.__version__,
    )


def format_latency(seconds: float) -> str:
    """Format a gateway latency in whole milliseconds.

    discord.py reports ``inf`` until the first heartbeat is acknowledged (up to about 40 seconds
    after connecting) and ``nan`` with no connection at all; rounding either raises, so they
    render as ``n/a`` instead.
    """
    if not math.isfinite(seconds):
        return "n/a"
    return f"{round(seconds * 1000)} ms"
