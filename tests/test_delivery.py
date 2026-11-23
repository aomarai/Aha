"""Delivery must only pass arguments that discord.py's ``send()`` really accepts.

The fakes used by the cog tests take any keyword, so they cannot notice a wrong one. In production a
bad argument (``enforce_nonce``, which ``send()`` does not have) raised ``TypeError`` for every
scheduled message and no reminder was delivered. These tests check against the real signatures.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, cast

import discord

from aha.delivery import send_to_user


class Recorder(discord.abc.Messageable):
    """A channel that records the keywords of ``send`` and checks them against the real method."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def _get_channel(self) -> Any:  # pragma: no cover - never reached
        raise NotImplementedError

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        real = inspect.signature(discord.abc.Messageable.send)
        # Binding raises TypeError for any keyword the real send() does not take.
        real.bind(self, content, **kwargs)
        self.calls.append(kwargs)


class Bot:
    def __init__(self, channel: Recorder, user: Recorder) -> None:
        self.channel = channel
        self.user = user

    def get_channel(self, channel_id: int, /) -> Any:
        return self.channel

    async def fetch_user(self, user_id: int, /) -> Any:
        return self.user


def test_every_argument_is_one_the_real_send_accepts_in_a_channel_and_in_a_dm() -> None:
    channel, user = Recorder(), Recorder()
    bot = Bot(channel, user)

    asyncio.run(
        send_to_user(cast("Any", bot), user_id=33, text="hi", channel_id=22, nonce=7, label="x")
    )
    asyncio.run(
        send_to_user(
            cast("Any", bot), user_id=33, text="hi", channel_id=None, nonce="end9", label="x"
        )
    )

    assert [sorted(call) for call in channel.calls] == [["allowed_mentions", "nonce"]]
    assert [sorted(call) for call in user.calls] == [["allowed_mentions", "nonce"]]
    assert channel.calls[0]["nonce"] == 7
    assert user.calls[0]["nonce"] == "end9"


def test_the_real_send_has_a_nonce_and_no_enforce_nonce() -> None:
    names = set(inspect.signature(discord.abc.Messageable.send).parameters)

    assert "nonce" in names
    assert "enforce_nonce" not in names  # discord.py enforces any nonce itself
