"""Tests for :mod:`aha.logging_setup`."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from aha.logging_setup import configure_logging

#: Named loggers whose level ``configure_logging`` mutates.
_NAMED_LOGGERS = ("discord", "aiohttp")


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Iterator[None]:
    """Snapshot and restore global logging state around each test.

    ``configure_logging`` replaces the root logger's handlers and level and
    changes the level of named loggers. Without this, that state leaks into
    whichever test runs next and makes the suite order-dependent.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_named = {name: logging.getLogger(name).level for name in _NAMED_LOGGERS}

    yield

    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, level in saved_named.items():
        logging.getLogger(name).setLevel(level)


def test_configure_logging_sets_root_level() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG

    configure_logging("WARNING")
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_quiets_aiohttp() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger("aiohttp").level == logging.WARNING
