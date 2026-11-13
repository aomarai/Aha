"""Tests for :mod:`aha.logging_setup`."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from aha import logctx
from aha.logging_setup import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
    static_fields_from_env,
)

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


def _record(
    message: str = "hello", *args: object, exc_info: Any = None, **extra: object
) -> logging.LogRecord:
    record = logging.LogRecord("aha.sample", logging.WARNING, __file__, 1, message, args, exc_info)
    record.created = 1_700_000_000.123456
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_the_fixed_keys() -> None:
    line = JsonFormatter().format(_record("count=%d", 3))

    assert json.loads(line) == {
        "ts": "2023-11-14T22:13:20.123+00:00",
        "level": "WARNING",
        "logger": "aha.sample",
        "msg": "count=3",
    }


def test_json_formatter_is_a_single_line() -> None:
    line = JsonFormatter().format(_record("line one\nline two"))

    assert "\n" not in line
    assert json.loads(line)["msg"] == "line one\nline two"


def test_json_formatter_adds_static_and_context_fields() -> None:
    token = logctx.bind(shard_id=1, guild_id=2, user_id=3, command="ping")
    try:
        line = JsonFormatter(static={"pod": "aha-0", "version": "1.2.3"}).format(_record())
    finally:
        logctx.reset(token)

    payload = json.loads(line)
    assert payload["pod"] == "aha-0"
    assert payload["version"] == "1.2.3"
    assert (payload["shard_id"], payload["guild_id"], payload["user_id"]) == (1, 2, 3)
    assert payload["command"] == "ping"


def test_json_formatter_includes_extra_fields_but_not_logrecord_internals() -> None:
    payload = json.loads(JsonFormatter().format(_record(outcome="ok", duration_ms=12)))

    assert payload["outcome"] == "ok"
    assert payload["duration_ms"] == 12
    assert set(payload) == {"ts", "level", "logger", "msg", "outcome", "duration_ms"}


def test_json_formatter_renders_non_serialisable_extras_as_strings() -> None:
    payload = json.loads(JsonFormatter().format(_record(thing=frozenset({1}))))

    assert payload["thing"] == "frozenset({1})"


def test_json_formatter_includes_a_traceback() -> None:
    try:
        raise ValueError("kaput")
    except ValueError:
        record = _record("failed", exc_info=sys.exc_info())

    payload = json.loads(JsonFormatter().format(record))

    assert payload["exc"].startswith("Traceback (most recent call last):")
    assert payload["exc"].endswith("ValueError: kaput")


def test_json_formatter_reuses_cached_exception_text() -> None:
    record = _record("failed")
    record.exc_text = "cached traceback"

    assert json.loads(JsonFormatter().format(record))["exc"] == "cached traceback"


def test_json_formatter_masks_secrets_in_messages_and_tracebacks() -> None:
    try:
        raise ValueError("bad token s3cr3t.token")
    except ValueError:
        record = _record("login with %s", "s3cr3t.token", exc_info=sys.exc_info())

    line = JsonFormatter(secrets=("s3cr3t.token",)).format(record)

    assert "s3cr3t.token" not in line
    payload = json.loads(line)
    assert payload["msg"] == "login with ***"
    assert payload["exc"].endswith("ValueError: bad token ***")


def test_text_formatter_masks_secrets() -> None:
    line = TextFormatter(secrets=("s3cr3t",)).format(_record("token=%s", "s3cr3t"))

    assert line.endswith("WARNING  aha.sample: token=***")


def test_masking_ignores_empty_secrets_and_masks_the_longest_first() -> None:
    line = TextFormatter(secrets=("", "abc", "abcdef")).format(_record("abcdef and abc"))

    assert line.endswith("*** and ***")


def test_configure_logging_json_writes_json_lines_with_masked_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", log_format="json", secrets=("tok.en",), static={"pod": "aha-0"})

    logging.getLogger("aha.sample").info("using %s", "tok.en")

    payload = json.loads(capsys.readouterr().out)
    assert payload["msg"] == "using ***"
    assert payload["pod"] == "aha-0"
    assert payload["logger"] == "aha.sample"


def test_configure_logging_text_masks_the_token(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", secrets=("tok.en",))

    logging.getLogger("aha.sample").info("using %s", "tok.en")

    assert capsys.readouterr().out.endswith("INFO     aha.sample: using ***\n")


def test_static_fields_come_from_the_environment() -> None:
    env = {"HOSTNAME": "aha-1", "AHA_VERSION": "0.2.0", "AHA_COMMIT": "abc123", "OTHER": "x"}

    assert static_fields_from_env(env) == {"pod": "aha-1", "version": "0.2.0", "commit": "abc123"}


def test_static_fields_skip_unset_and_empty_variables() -> None:
    assert static_fields_from_env({"HOSTNAME": "", "AHA_VERSION": "0.2.0"}) == {"version": "0.2.0"}


def test_static_fields_default_to_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOSTNAME", "from-environ")
    monkeypatch.delenv("AHA_VERSION", raising=False)
    monkeypatch.delenv("AHA_COMMIT", raising=False)

    assert static_fields_from_env() == {"pod": "from-environ"}
