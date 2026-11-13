"""Shared reply helper for cog responses.

Discord parses mentions in message content by default, so a reply that includes
user-supplied text can ping roles, users, or ``@everyone``. Routing every cog
reply through :func:`reply` keeps that policy in one place.
"""

from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

__all__ = ["NO_MENTIONS", "reply"]

#: Reusable policy: never parse mentions in reply content.
NO_MENTIONS = discord.AllowedMentions.none()


async def reply(
    ctx: commands.Context[commands.Bot], content: str | None = None, **kwargs: Any
) -> None:
    """Reply to ``ctx`` with mentions disabled unless the caller overrides them.

    Args:
        ctx: The invocation context to reply to.
        content: Message content, if any. Embed-only replies pass ``embed=``.
        **kwargs: Forwarded to :meth:`discord.ext.commands.Context.reply`.
    """
    kwargs.setdefault("allowed_mentions", NO_MENTIONS)
    await ctx.reply(content, **kwargs)
