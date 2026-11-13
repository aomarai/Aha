"""Tests for the shared component helpers."""

from __future__ import annotations

import asyncio

from aha import logctx
from aha.components import NOT_YOURS, require_invoker
from tests.helpers import StubInteraction, as_interaction, assert_no_mentions


def test_the_owner_may_proceed_without_a_reply() -> None:
    interaction = StubInteraction(user_id=33)

    allowed = asyncio.run(require_invoker(as_interaction(interaction), 33))

    assert allowed is True
    assert interaction.sent == []


def test_anyone_else_is_refused_privately_with_the_default_message() -> None:
    interaction = StubInteraction(user_id=2)

    allowed = asyncio.run(require_invoker(as_interaction(interaction), 33))

    assert allowed is False
    ((kind, content, kwargs),) = interaction.sent
    assert (kind, content) == ("response", "This menu isn't yours.")
    assert content == NOT_YOURS
    assert kwargs["ephemeral"] is True
    assert_no_mentions(kwargs["allowed_mentions"])


def test_a_component_can_supply_its_own_refusal() -> None:
    interaction = StubInteraction(user_id=2)

    asyncio.run(require_invoker(as_interaction(interaction), 33, message="Not for you."))

    assert interaction.sent[0][1] == "Not for you."


def test_the_refusal_is_a_followup_when_the_interaction_was_already_answered() -> None:
    interaction = StubInteraction(user_id=2, done=True)

    asyncio.run(require_invoker(as_interaction(interaction), 33))

    assert interaction.sent[0][0] == "followup"


def test_both_outcomes_bind_the_interaction_log_context() -> None:
    async def scenario(user_id: int) -> dict[str, object]:
        await require_invoker(as_interaction(StubInteraction(user_id=user_id)), 33)
        return dict(logctx.current())

    for user_id in (33, 2):
        context = asyncio.run(scenario(user_id))
        assert context["user_id"] == user_id
        assert context["interaction_id"] == 777
