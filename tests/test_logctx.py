"""Tests for the per-invocation logging context."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from aha import logctx
from tests.helpers import Named, StubContext, StubInteraction, as_context, as_interaction


@pytest.fixture(autouse=True)
def _clean_context() -> Iterator[None]:
    token = logctx.bind()
    yield
    logctx.reset(token)


def test_the_context_is_empty_by_default() -> None:
    assert dict(logctx.current()) == {}


def test_bind_merges_fields_and_skips_none() -> None:
    logctx.bind(guild_id=1, command="ping")
    logctx.bind(guild_id=2, channel_id=None)

    assert dict(logctx.current()) == {"guild_id": 2, "command": "ping"}


def test_reset_restores_the_previous_context() -> None:
    logctx.bind(guild_id=1)
    token = logctx.bind(user_id=5)

    logctx.reset(token)

    assert dict(logctx.current()) == {"guild_id": 1}


def test_the_current_mapping_cannot_be_mutated() -> None:
    logctx.bind(guild_id=1)

    snapshot: Any = logctx.current()

    with pytest.raises(TypeError):
        snapshot["guild_id"] = 2


def test_bind_interaction_records_the_ids_of_a_guild_interaction() -> None:
    logctx.bind_interaction(as_interaction(StubInteraction()))

    assert dict(logctx.current()) == {
        "shard_id": 2,
        "guild_id": 11,
        "channel_id": 22,
        "user_id": 33,
        "interaction_id": 777,
        "command": "ping",
    }


def test_bind_interaction_puts_a_dm_on_shard_zero_and_omits_a_missing_command() -> None:
    logctx.bind_interaction(as_interaction(StubInteraction(guild=False, command=False)))

    assert dict(logctx.current()) == {
        "shard_id": 0,
        "channel_id": 22,
        "user_id": 33,
        "interaction_id": 777,
    }


def test_bind_context_records_the_ids_of_a_mention_command() -> None:
    logctx.bind_context(as_context(StubContext()))

    assert dict(logctx.current()) == {
        "shard_id": 2,
        "guild_id": 11,
        "channel_id": 22,
        "user_id": 33,
        "message_id": 44,
        "command": "ping",
    }


def test_bind_context_in_a_dm_uses_shard_zero_and_no_guild() -> None:
    logctx.bind_context(as_context(StubContext(guild=False, command=False)))

    assert dict(logctx.current()) == {
        "shard_id": 0,
        "channel_id": 22,
        "user_id": 33,
        "message_id": 44,
    }


def test_a_value_bound_in_one_task_is_invisible_to_a_sibling_task() -> None:
    seen: dict[str, dict[str, object]] = {}

    async def handler(name: str, user_id: int) -> None:
        logctx.bind(user_id=user_id)
        await asyncio.sleep(0)  # let the other task bind before reading
        seen[name] = dict(logctx.current())

    async def scenario() -> None:
        await asyncio.gather(
            asyncio.create_task(handler("a", 1)), asyncio.create_task(handler("b", 2))
        )

    asyncio.run(scenario())

    assert seen == {"a": {"user_id": 1}, "b": {"user_id": 2}}
    assert dict(logctx.current()) == {}


def test_bind_interaction_derives_the_shard_for_an_uncached_guild() -> None:
    interaction = StubInteraction(guild_cached=False)
    interaction.guild_id = (5 << 22) + 123  # (guild_id >> 22) == 5, and 5 % 4 shards == 1

    logctx.bind_interaction(as_interaction(interaction))

    assert logctx.current()["shard_id"] == 1
    assert logctx.current()["guild_id"] == (5 << 22) + 123


def test_bind_interaction_assumes_one_shard_when_the_client_has_no_shard_count() -> None:
    interaction = StubInteraction(guild_cached=False)
    interaction.client = Named(shard_count=None)

    logctx.bind_interaction(as_interaction(interaction))

    assert logctx.current()["shard_id"] == 0
