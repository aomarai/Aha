"""Per-invocation logging context carried in a :class:`contextvars.ContextVar`.

Every interaction (and every ``@Aha`` mention command) runs in its own asyncio task, so a
value bound at its entry point is visible to every log call made while handling it, with
no plumbing through call sites, and is invisible to unrelated invocations. The JSON log
formatter reads :func:`current` when it renders a record.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token
from types import MappingProxyType
from typing import Any

import discord
from discord.ext import commands

__all__ = ["bind", "bind_context", "bind_interaction", "current", "reset"]

#: DMs (and anything without a guild) are delivered on shard 0.
_DM_SHARD_ID = 0

_EMPTY: Mapping[str, object] = MappingProxyType({})
_context: ContextVar[Mapping[str, object]] = ContextVar("aha_log_context", default=_EMPTY)


def current() -> Mapping[str, object]:
    """Return the fields bound for the invocation being handled (empty outside one)."""
    return _context.get()


def bind(**fields: object) -> Token[Mapping[str, object]]:
    """Add ``fields`` (``None`` values are skipped) and return a token for :func:`reset`."""
    merged = {
        **_context.get(),
        **{key: value for key, value in fields.items() if value is not None},
    }
    return _context.set(MappingProxyType(merged))


def reset(token: Token[Mapping[str, object]]) -> None:
    """Undo a :func:`bind`. Production code rarely needs this: a task's context dies with it."""
    _context.reset(token)


def bind_interaction(interaction: discord.Interaction[Any]) -> Token[Mapping[str, object]]:
    """Bind the ids describing a slash (or component) interaction."""
    guild = interaction.guild
    command = interaction.command
    return bind(
        shard_id=_DM_SHARD_ID if guild is None else guild.shard_id,
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
        user_id=interaction.user.id,
        interaction_id=interaction.id,
        command=None if command is None else command.qualified_name,
    )


def bind_context(ctx: commands.Context[Any]) -> Token[Mapping[str, object]]:
    """Bind the ids describing a mention-invoked command."""
    guild = ctx.guild
    command = ctx.command
    return bind(
        shard_id=_DM_SHARD_ID if guild is None else guild.shard_id,
        guild_id=None if guild is None else guild.id,
        channel_id=ctx.channel.id,
        user_id=ctx.author.id,
        message_id=ctx.message.id,
        command=None if command is None else command.qualified_name,
    )
