"""Shared pieces for message components (buttons and select menus).

The convention, in full, is in ``docs/components.md``. In short: a component must keep working
after a restart and on whichever process receives the click, so its state lives in its
``custom_id`` (or, for state too big for 100 characters, in the database), never in a ``View``
instance, and every click re-checks who is allowed to make it.
"""

from __future__ import annotations

import discord

from aha import logctx
from aha.replies import respond

__all__ = ["NOT_YOURS", "require_invoker"]

#: Reply to someone clicking a component that belongs to another user.
NOT_YOURS = "This menu isn't yours."


async def require_invoker(
    interaction: discord.Interaction, owner_id: int, *, message: str = NOT_YOURS
) -> bool:
    """Return True when the clicker is ``owner_id``; otherwise tell them so, privately.

    Also binds the interaction's log context: a component callback never passes the command
    tree's check, which is where slash invocations bind it, so this is the one place every
    component's checks start.
    """
    logctx.bind_interaction(interaction)
    if interaction.user.id == owner_id:
        return True
    await respond(interaction, message, ephemeral=True)
    return False
