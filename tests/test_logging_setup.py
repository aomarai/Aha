"""Tests for :mod:`aha.logging_setup`."""

from __future__ import annotations

import logging

from aha.logging_setup import configure_logging


def test_configure_logging_sets_root_level() -> None:
    original = logging.getLogger().level
    try:
        configure_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

        configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING
    finally:
        logging.getLogger().setLevel(original)


def test_configure_logging_quiets_aiohttp() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger("aiohttp").level == logging.WARNING
