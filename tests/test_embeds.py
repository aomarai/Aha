"""Tests for the embed building module."""

from __future__ import annotations

import discord

from aha import embeds


def _color_value(e: discord.Embed) -> int:
    """Return the embed color as an int, asserting it is set."""
    assert e.color is not None
    return e.color.value


def test_info_embed_has_blurple_color() -> None:
    e = embeds.info("hello")
    assert _color_value(e) == embeds.Colors.INFO
    assert e.description == "hello"


def test_success_embed_has_green_color() -> None:
    e = embeds.success("done")
    assert _color_value(e) == embeds.Colors.SUCCESS
    assert e.description == "done"


def test_warning_embed_has_yellow_color() -> None:
    e = embeds.warning("careful")
    assert _color_value(e) == embeds.Colors.WARNING
    assert e.description == "careful"


def test_error_embed_has_red_color() -> None:
    e = embeds.error("failed")
    assert _color_value(e) == embeds.Colors.ERROR
    assert e.description == "failed"


def test_status_embed_has_purple_color() -> None:
    e = embeds.status("latency 42 ms")
    assert _color_value(e) == embeds.Colors.STATUS
    assert e.description == "latency 42 ms"


def test_title_is_set_when_provided() -> None:
    e = embeds.info("body", title="Heading")
    assert e.title == "Heading"


def test_title_is_none_when_not_provided() -> None:
    e = embeds.info("body")
    assert e.title is None


def test_fields_are_added() -> None:
    e = embeds.info(
        "body",
        fields=[("Latency", "42 ms", True), ("Uptime", "3d 4h", False)],
    )
    assert len(e.fields) == 2
    assert e.fields[0].name == "Latency"
    assert e.fields[0].value == "42 ms"
    assert e.fields[0].inline is True
    assert e.fields[1].name == "Uptime"
    assert e.fields[1].inline is False


def test_no_fields_by_default() -> None:
    e = embeds.success("ok")
    assert len(e.fields) == 0


def test_embed_has_footer() -> None:
    e = embeds.info("x")
    assert e.footer is not None
    assert e.footer.text == "Aha"


def test_embed_has_timestamp() -> None:
    e = embeds.error("oops")
    assert e.timestamp is not None


def test_all_builders_accept_fields_and_title() -> None:
    for builder in (embeds.info, embeds.success, embeds.warning, embeds.error, embeds.status):
        e = builder("d", title="T", fields=[("F", "V", True)])
        assert e.title == "T"
        assert len(e.fields) == 1


def test_embed_is_discord_embed() -> None:
    e = embeds.info("x")
    assert isinstance(e, discord.Embed)
