"""``/forgetme``: delete everything Aha stores about the invoker (#99).

A destructive action, so it takes two steps (docs/components.md rule 3): the command shows what
will be deleted with a confirm button that only the invoker can press and that expires after five
minutes. The button is a persistent ``DynamicItem`` (state in its ``custom_id``), so it keeps
working after a restart and on whichever pod receives the click.
"""

from __future__ import annotations

import re
import time
from typing import Any

import discord
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.components import require_invoker
from aha.guildsettings_store import GuildSettingsStore
from aha.jobstore import JobStore
from aha.privacy import purge_user
from aha.replies import edit_message, reply

__all__ = ["CONFIRM_WINDOW", "Forget", "ForgetButton", "setup"]

#: Seconds the confirm button stays valid.
CONFIRM_WINDOW = 300

_UNAVAILABLE = (
    "Aha keeps no personal data in a database in this deployment, so there is nothing to delete. "
    "Operational logs are covered by the privacy policy."
)


def now() -> float:
    """Unix time in seconds (a seam for tests)."""
    return time.time()


class ForgetButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"aha:forget:(?P<owner>\d+):(?P<expires>\d+)",
):
    """The confirm button; ``aha:forget:<owner id>:<expiry, unix seconds>``."""

    def __init__(self, owner_id: int, expires: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Delete my data",
                style=discord.ButtonStyle.danger,
                custom_id=f"aha:forget:{owner_id}:{expires}",
            )
        )
        self.owner_id = owner_id
        self.expires = expires

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> ForgetButton:
        """Rebuild the button from a ``custom_id`` that matched the template."""
        return cls(int(match["owner"]), int(match["expires"]))

    async def interaction_check(self, interaction: discord.Interaction[Any]) -> bool:
        """Only the person asking to be forgotten may confirm."""
        return await require_invoker(
            interaction, self.owner_id, message="This confirmation isn't yours."
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        """Delete the invoker's data, unless the confirmation expired."""
        if now() > self.expires:
            await edit_message(
                interaction,
                embed=embeds.error("This confirmation expired. Run `/forgetme` again."),
                view=None,
            )
            return
        bot: AhaBot = interaction.client
        if bot.db is None:
            await edit_message(interaction, embed=embeds.info(_UNAVAILABLE), view=None)
            return
        result = await purge_user(JobStore(bot.db), GuildSettingsStore(bot.db), self.owner_id)
        text = (
            f"Done. Deleted {result.jobs} reminder(s) and scheduled job(s) and removed your id "
            f"from {result.settings} server setting(s)."
        )
        await edit_message(interaction, embed=embeds.success(text), view=None)


class Forget(commands.Cog):
    """The data-deletion command."""

    def __init__(self, bot: AhaBot) -> None:
        self.bot = bot

    @hybrid_command(
        name="forgetme",
        description="Delete everything Aha stores about you.",
        extras={"category": "Info"},
    )
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def forgetme(self, ctx: commands.Context[commands.Bot]) -> None:
        """Explain what will be deleted and ask for confirmation (private to the invoker)."""
        if self.bot.db is None:
            await reply(ctx, embed=embeds.info(_UNAVAILABLE), ephemeral=True)
            return
        text = (
            "This permanently deletes your pending reminders and any other scheduled jobs, and "
            "removes your id from server settings you changed (the settings stay). It cannot be "
            "undone. Messages you sent and anything Discord stores are not affected.\n\n"
            "Press the button within 5 minutes to confirm."
        )
        view = discord.ui.View(timeout=None)
        view.add_item(ForgetButton(ctx.author.id, int(now()) + CONFIRM_WINDOW))
        await reply(
            ctx, embed=embeds.error(text, title="Delete your data?"), view=view, ephemeral=True
        )


async def setup(bot: AhaBot) -> None:
    """Add the cog and register the persistent confirm button."""
    bot.add_dynamic_items(ForgetButton)
    await bot.add_cog(Forget(bot))
