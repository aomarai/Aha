"""Helpers for showing untrusted text (user input, remote page titles) safely in a reply."""

from __future__ import annotations

import discord

__all__ = ["clip", "code", "pack", "plain"]


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


def pack(lines: list[str], limit: int) -> list[str]:
    """Join ``lines`` into pages of at most ``limit`` characters (a longer line gets its own)."""
    pages: list[str] = []
    for line in lines:
        if pages and len(pages[-1]) + 1 + len(line) <= limit:
            pages[-1] += "\n" + line
        else:
            pages.append(line)
    return pages
