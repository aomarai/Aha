"""Consistent embed building for bot responses.

Every command response that carries structured content uses one of the builders
here rather than a raw ``discord.Embed`` or plain text, so the bot presents a
consistent visual identity across all commands.

Usage in a cog::

    from aha import embeds

    await ctx.reply(embed=embeds.status(f"WebSocket latency: {ms} ms"))
    await interaction.response.send_message(
        embed=embeds.info("Done.", title="Result")
    )

The builders accept an optional ``title`` and a sequence of ``(name, value,
inline)`` field tuples for structured content. All responses carry the bot's
brand color, a UTC timestamp, and a footer identifying the bot.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import discord

__all__ = ["Colors", "error", "info", "status", "success", "warning"]

#: A field is a (name, value, inline) triple matching ``Embed.add_field``.
Field = tuple[str, str, bool]


class Colors:
    """Accent colors for response types (hex integers)."""

    INFO = 0x5865F2  # Discord blurple — general information
    SUCCESS = 0x57F287  # Green — confirmation, positive result
    WARNING = 0xFEE75C  # Yellow — caution, partial failure
    ERROR = 0xED4245  # Red — failure, negative result
    STATUS = 0xB26FF2  # Purple — diagnostics, latency, health


def _build(description: str, *, color: int) -> discord.Embed:
    """Common embed skeleton: timestamp + bot footer."""
    e = discord.Embed(description=description, color=color)
    e.timestamp = datetime.now(tz=UTC)
    e.set_footer(text="Aha")
    return e


def _typed(
    description: str,
    *,
    color: int,
    title: str | None = None,
    fields: Sequence[Field] = (),
) -> discord.Embed:
    """Build a typed embed with optional title and inline fields."""
    e = _build(description, color=color)
    if title is not None:
        e.title = title
    for name, value, inline in fields:
        e.add_field(name=name, value=value, inline=inline)
    return e


def info(
    description: str,
    *,
    title: str | None = None,
    fields: Sequence[Field] = (),
) -> discord.Embed:
    """General informational response.

    Args:
        description: Main body text (required).
        title: Optional heading above the body.
        fields: Zero or more ``(name, value, inline)`` triples for structured
            content below the body.

    Returns:
        A blurple embed ready to pass as ``embed=`` to a reply call.
    """
    return _typed(description, color=Colors.INFO, title=title, fields=fields)


def success(
    description: str,
    *,
    title: str | None = None,
    fields: Sequence[Field] = (),
) -> discord.Embed:
    """Confirmation or positive result.

    Args:
        description: Main body text (required).
        title: Optional heading above the body.
        fields: Zero or more ``(name, value, inline)`` triples for structured
            content below the body.

    Returns:
        A green embed ready to pass as ``embed=`` to a reply call.
    """
    return _typed(description, color=Colors.SUCCESS, title=title, fields=fields)


def warning(
    description: str,
    *,
    title: str | None = None,
    fields: Sequence[Field] = (),
) -> discord.Embed:
    """Caution or partial failure.

    Args:
        description: Main body text (required).
        title: Optional heading above the body.
        fields: Zero or more ``(name, value, inline)`` triples for structured
            content below the body.

    Returns:
        A yellow embed ready to pass as ``embed=`` to a reply call.
    """
    return _typed(description, color=Colors.WARNING, title=title, fields=fields)


def error(
    description: str,
    *,
    title: str | None = None,
    fields: Sequence[Field] = (),
) -> discord.Embed:
    """Failure or negative result.

    Args:
        description: Main body text (required).
        title: Optional heading above the body.
        fields: Zero or more ``(name, value, inline)`` triples for structured
            content below the body.

    Returns:
        A red embed ready to pass as ``embed=`` to a reply call.
    """
    return _typed(description, color=Colors.ERROR, title=title, fields=fields)


def status(
    description: str,
    *,
    title: str | None = None,
    fields: Sequence[Field] = (),
) -> discord.Embed:
    """Status, latency, or diagnostic response.

    Args:
        description: Main body text (required).
        title: Optional heading above the body.
        fields: Zero or more ``(name, value, inline)`` triples for structured
            content below the body.

    Returns:
        A purple embed ready to pass as ``embed=`` to a reply call.
    """
    return _typed(description, color=Colors.STATUS, title=title, fields=fields)
