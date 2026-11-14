"""Tests for the duration parser."""

from __future__ import annotations

from datetime import timedelta

import pytest

from aha.durations import DurationError, parse_duration

USAGE = "Use a duration like `30m`, `2h` or `1d 12h`."


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("1s", 1),
        ("30m", 1800),
        ("2h", 7200),
        ("1d", 86400),
        ("1w", 604800),
        ("1h30m", 5400),
        ("1d 12h", 129600),
        ("  90 MIN ", 5400),
        ("2 hours", 7200),
        ("3 days", 259200),
        ("45 secs", 45),
        ("1 week 2 days", 777600),
        ("1sec", 1),
        ("1second", 1),
        ("1seconds", 1),
        ("1mins", 60),
        ("1minute", 60),
        ("1minutes", 60),
        ("1hr", 3600),
        ("1hrs", 3600),
        ("1hour", 3600),
        ("1day", 86400),
        ("1weeks", 604800),
        ("1week", 604800),
    ],
)
def test_durations_parse_to_the_right_length(text: str, seconds: int) -> None:
    assert parse_duration(text) == timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "text", ["", "   ", "soon", "5", "h", "5x", "5 parsecs", "1h and 2m", "-5m"]
)
def test_text_that_is_not_a_duration_is_rejected_with_usage(text: str) -> None:
    with pytest.raises(DurationError) as caught:
        parse_duration(text)

    assert str(caught.value) == USAGE
    assert caught.value.key == "duration.invalid"


def test_a_zero_length_is_rejected() -> None:
    with pytest.raises(DurationError) as caught:
        parse_duration("0m")

    assert str(caught.value) == "The duration must be longer than zero."
    assert caught.value.key == "duration.zero"


def test_an_absurdly_large_number_is_rejected_not_an_overflow() -> None:
    with pytest.raises(DurationError) as caught:
        parse_duration("99999999999999999999d")

    assert str(caught.value) == USAGE
    assert caught.value.key == "duration.invalid"


def test_the_largest_accepted_number_has_seven_digits() -> None:
    assert parse_duration("9999999s") == timedelta(seconds=9_999_999)
    with pytest.raises(DurationError):
        parse_duration("10000000s")


def test_an_unknown_unit_inside_a_valid_shape_is_the_same_usage_error() -> None:
    with pytest.raises(DurationError) as caught:
        parse_duration("5 parsecs")

    assert (str(caught.value), caught.value.key) == (USAGE, "duration.invalid")
