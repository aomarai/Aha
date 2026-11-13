"""Tests for the shared reply helper."""

from __future__ import annotations

import asyncio

import discord

from aha import embeds
from aha.replies import reply
from tests.helpers import StubContext, as_context


def test_reply_disables_mentions_by_default() -> None:
    ctx = StubContext()

    asyncio.run(reply(as_context(ctx), "hi @everyone"))

    content, kwargs = ctx.replies[0]
    assert content == "hi @everyone"
    allowed = kwargs["allowed_mentions"]
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False
    assert allowed.replied_user is False


def test_reply_keeps_an_explicit_mention_policy() -> None:
    ctx = StubContext()
    custom = discord.AllowedMentions(users=True)

    asyncio.run(reply(as_context(ctx), "hi", allowed_mentions=custom))

    _, kwargs = ctx.replies[0]
    assert kwargs["allowed_mentions"] is custom


def test_reply_forwards_other_keywords() -> None:
    ctx = StubContext()
    embed = embeds.info("x")

    asyncio.run(reply(as_context(ctx), embed=embed, ephemeral=True))

    content, kwargs = ctx.replies[0]
    assert content is None
    assert kwargs["embed"] is embed
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False
