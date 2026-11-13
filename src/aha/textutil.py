"""Helpers for showing untrusted text (user input, remote page titles) safely in a reply."""

from __future__ import annotations

import discord

__all__ = ["clip", "code", "plain"]


def plain(text: str) -> str:
    """Make ``text`` safe to show as ordinary message text: no markdown, mentions or code."""
    # Backticks go first: escaping them would leave a stray backslash behind.
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text.replace("`", "'")))


def code(text: str) -> str:
    """Make ``text`` safe to show inside an inline code span (which neutralises markdown)."""
    return text.replace("`", "'")


def clip(text: str, limit: int) -> str:
    """Shorten ``text`` to at most ``limit`` characters, ending in an ellipsis if it was cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"
