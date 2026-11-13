"""Shared reply helper for cog responses.

Discord parses mentions in message content by default, so a reply that includes
user-supplied text can ping roles, users, or ``@everyone``. Routing every cog
reply through :func:`reply` keeps that policy in one place.
"""

from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

__all__ = ["edit_message", "reply", "respond"]


async def reply(
    ctx: commands.Context[commands.Bot], content: str | None = None, **kwargs: Any
) -> None:
    """Reply to ``ctx`` with mentions disabled.

    Args:
        ctx: The invocation context to reply to.
        content: Message content, if any. Embed-only replies pass ``embed=``.
        **kwargs: Forwarded to :meth:`discord.ext.commands.Context.reply`.
    """
    kwargs["allowed_mentions"] = discord.AllowedMentions.none()
    await ctx.reply(content, **kwargs)


async def respond(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    ephemeral: bool = False,
    **kwargs: Any,
) -> None:
    """Send an interaction response with mentions disabled.

    Uses a followup when the interaction has already been acknowledged, which is
    the same pattern the error handler needs.

    Args:
        interaction: The interaction to respond to.
        content: Message content, if any. Embed-only responses pass ``embed=``.
        ephemeral: Whether the response is visible only to the invoker.
        **kwargs: Forwarded to ``send_message`` / ``followup.send``.
    """
    kwargs["allowed_mentions"] = discord.AllowedMentions.none()
    if interaction.response.is_done():
        if content is None:
            await interaction.followup.send(ephemeral=ephemeral, **kwargs)
        else:
            await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)


async def edit_message(
    interaction: discord.Interaction, content: str | None = None, **kwargs: Any
) -> None:
    """Edit the interaction's message with mentions disabled."""
    kwargs["allowed_mentions"] = discord.AllowedMentions.none()
    await interaction.response.edit_message(content=content, **kwargs)
