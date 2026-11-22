"""Sending a scheduled message to one person: in a channel, else by DM, else dropped.

Shared by ``/remind`` and ``/watch``. Only the recipient can be pinged. A missing channel or
permission falls back to a DM, and closed DMs drop the message (logged). Any other Discord error
propagates so the scheduler retries. The nonce is *enforced*, so Discord drops a repeat of the same
message sent within a few minutes: a redelivery after a crash does not ping twice. Nonces must not
collide between kinds of message that can reach the same channel.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import discord

__all__ = ["Courier", "send_to_user"]

log = logging.getLogger(__name__)


class Courier(Protocol):
    """What delivery needs of the bot."""

    def get_channel(self, channel_id: int, /) -> Any:
        """A cached channel, or ``None``."""

    async def fetch_user(self, user_id: int, /) -> Any:
        """The user, over REST."""


async def send_to_user(
    bot: Courier, *, user_id: int, text: str, channel_id: object, nonce: int | str, label: str
) -> None:
    """Send ``text`` to ``user_id`` in ``channel_id`` if possible, else by DM.

    ``label`` names the job in log lines (``"Reminder 12"``).
    """
    mentions = discord.AllowedMentions(
        everyone=False, roles=False, users=[discord.Object(id=user_id)]
    )
    send: dict[str, Any] = {
        "allowed_mentions": mentions,
        "nonce": nonce,
        "enforce_nonce": True,
    }
    channel = bot.get_channel(channel_id) if isinstance(channel_id, int) else None
    if isinstance(channel, discord.abc.Messageable):
        try:
            await channel.send(text, **send)
            return
        except discord.Forbidden, discord.NotFound:
            log.info("%s: channel unavailable, falling back to a DM", label)
    try:
        user = await bot.fetch_user(user_id)
        await user.send(text, **send)
    except discord.Forbidden, discord.NotFound:
        log.warning("%s dropped: the channel and the DM are both unreachable", label)
