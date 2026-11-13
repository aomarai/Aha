"""Tests for the shared reply helpers."""

from __future__ import annotations

import asyncio

import discord

from aha import embeds
from aha.replies import edit_message, reply, respond
from tests.helpers import (
    StubContext,
    StubInteraction,
    as_context,
    as_interaction,
    assert_no_mentions,
)


def test_reply_disables_mentions_by_default() -> None:
    ctx = StubContext()

    asyncio.run(reply(as_context(ctx), "hi @everyone"))

    content, kwargs = ctx.replies[0]
    assert content == "hi @everyone"
    assert_no_mentions(kwargs["allowed_mentions"])


def test_respond_sends_with_mentions_disabled() -> None:
    interaction = StubInteraction()

    asyncio.run(respond(as_interaction(interaction), "hi @everyone", ephemeral=True))

    kind, content, kwargs = interaction.sent[0]
    assert (kind, content) == ("response", "hi @everyone")
    assert kwargs["ephemeral"] is True
    assert_no_mentions(kwargs["allowed_mentions"])


def test_respond_uses_a_followup_after_acknowledgement() -> None:
    interaction = StubInteraction(done=True)

    asyncio.run(respond(as_interaction(interaction), "later"))

    kind, content, kwargs = interaction.sent[0]
    assert kind == "followup"
    assert content == "later"
    assert kwargs["ephemeral"] is False
    assert_no_mentions(kwargs["allowed_mentions"])


def test_respond_forwards_embed_only_followups() -> None:
    interaction = StubInteraction(done=True)
    embed = embeds.info("x")

    asyncio.run(respond(as_interaction(interaction), embed=embed))

    kind, content, kwargs = interaction.sent[0]
    assert (kind, content) == ("followup", None)
    assert kwargs["embed"] is embed
    assert kwargs["ephemeral"] is False
    assert_no_mentions(kwargs["allowed_mentions"])


def test_edit_message_keeps_the_mention_policy() -> None:
    interaction = StubInteraction()

    asyncio.run(edit_message(as_interaction(interaction), "page 2", view=None))

    (edited,) = interaction.edited
    assert edited["content"] == "page 2"
    assert edited["view"] is None
    assert_no_mentions(edited["allowed_mentions"])


def test_reply_overrides_a_caller_mention_policy() -> None:
    ctx = StubContext()

    asyncio.run(reply(as_context(ctx), "hi", allowed_mentions=discord.AllowedMentions.all()))

    _, kwargs = ctx.replies[0]
    assert_no_mentions(kwargs["allowed_mentions"])


def test_reply_forwards_other_keywords() -> None:
    ctx = StubContext()
    embed = embeds.info("x")

    asyncio.run(reply(as_context(ctx), embed=embed, ephemeral=True))

    content, kwargs = ctx.replies[0]
    assert content is None
    assert kwargs["embed"] is embed
    assert kwargs["ephemeral"] is True
    assert_no_mentions(kwargs["allowed_mentions"])


def test_each_reply_gets_a_fresh_policy() -> None:
    first = StubContext()
    second = StubContext()

    asyncio.run(reply(as_context(first), "a"))
    asyncio.run(reply(as_context(second), "b"))

    first_policy = first.replies[0][1]["allowed_mentions"]
    second_policy = second.replies[0][1]["allowed_mentions"]
    assert first_policy is not second_policy

    # Mutating one call's policy must not affect the next reply.
    first_policy.users = True
    third = StubContext()
    asyncio.run(reply(as_context(third), "c"))
    assert_no_mentions(third.replies[0][1]["allowed_mentions"])
