"""Tests for build identity and latency formatting."""

from __future__ import annotations

import math
import platform

import discord
import pytest

from aha import __version__
from aha.botinfo import UNKNOWN, BuildInfo, build_info, format_latency


def test_build_info_reads_the_image_build_identity() -> None:
    info = build_info({"AHA_VERSION": "1.2.3", "AHA_COMMIT": "0123456789abcdef"})

    assert info == BuildInfo(
        version="1.2.3",
        commit="0123456789abcdef",
        python=platform.python_version(),
        discord_py=discord.__version__,
    )
    assert info.short_commit == "0123456"


def test_a_local_run_falls_back_to_the_package_version_and_unknown_commit() -> None:
    info = build_info({})

    assert info.version == __version__
    assert info.commit == UNKNOWN
    assert info.short_commit == "unknown"


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_blank_values_count_as_unset(blank: str) -> None:
    info = build_info({"AHA_VERSION": blank, "AHA_COMMIT": blank})

    assert (info.version, info.commit) == (__version__, UNKNOWN)


def test_values_are_trimmed() -> None:
    info = build_info({"AHA_VERSION": " 1.0.0 ", "AHA_COMMIT": " abcdef0123 "})

    assert (info.version, info.commit) == ("1.0.0", "abcdef0123")


def test_a_short_commit_is_not_padded_or_cut() -> None:
    assert build_info({"AHA_COMMIT": "abc"}).short_commit == "abc"
    assert build_info({"AHA_COMMIT": "abcdefg"}).short_commit == "abcdefg"


def test_build_info_defaults_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AHA_VERSION", "9.9.9")
    monkeypatch.setenv("AHA_COMMIT", "feedface00")

    info = build_info()

    assert (info.version, info.short_commit) == ("9.9.9", "feedfac")


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0.042, "42 ms"), (0.0, "0 ms"), (0.0004, "0 ms"), (1.5, "1500 ms"), (0.0425, "42 ms")],
)
def test_format_latency_rounds_to_whole_milliseconds(seconds: float, text: str) -> None:
    assert format_latency(seconds) == text


@pytest.mark.parametrize("seconds", [math.inf, -math.inf, math.nan])
def test_format_latency_survives_the_values_discord_py_reports_before_a_heartbeat(
    seconds: float,
) -> None:
    assert format_latency(seconds) == "n/a"
