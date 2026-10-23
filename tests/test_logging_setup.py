"""Tests for :mod:`aha.logging_setup`."""

from __future__ import annotations

import logging
import re
import sys
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
    # Other tests (e.g. ``main``) run the real ``configure_logging`` and leak
    # levels; start from a clean slate so these tests observe only their own call.
    for name in _NAMED_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)

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


def test_configure_logging_defaults_to_info() -> None:
    configure_logging()

    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger("discord").level == logging.INFO


def test_configure_logging_applies_level_to_discord() -> None:
    configure_logging("ERROR")

    assert logging.getLogger("discord").level == logging.ERROR


def test_configure_logging_keeps_existing_loggers_enabled() -> None:
    existing = logging.getLogger("tests.preexisting")

    configure_logging("INFO")

    assert not existing.disabled


def test_configure_logging_installs_a_stdout_console_handler() -> None:
    configure_logging("INFO")

    (handler,) = logging.getLogger().handlers
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout


def test_configure_logging_formats_records(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    logging.getLogger("aha.sample").info("hello")

    # asctime uses ``%Y-%m-%d %H:%M:%S``; levelname is left-padded to 8.
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} INFO     aha\.sample: hello\n",
        capsys.readouterr().out,
    )
