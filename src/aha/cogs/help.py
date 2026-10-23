"""Paginated help command listing the bot's commands."""

from __future__ import annotations

import discord
from discord.ext import commands

from aha.commands import hybrid_command

__all__ = ["Help", "HelpPaginator", "paginate", "setup"]

#: Max characters of command text per page. Discord caps a message at 2000; the
#: remainder is headroom for the "Page x/y" footer.
PAGE_LIMIT = 1900


def paginate(lines: list[str], limit: int = PAGE_LIMIT) -> list[str]:
    """Greedily pack ``lines`` into newline-joined pages of at most ``limit`` characters.

    A single line longer than ``limit`` is truncated with an ellipsis rather than
    split, so every page stays within the limit.
    """
    pages: list[str] = []
    current: list[str] = []
    for line in lines:
        if len(line) > limit:
            line = line[: limit - 1] + "…"
        if current and len("\n".join([*current, line])) > limit:
            pages.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        pages.append("\n".join(current))
    return pages


class HelpPaginator(discord.ui.View):
    """Previous/Next buttons that flip through help pages in place."""

    def __init__(self, pages: list[str], owner_id: int) -> None:
        super().__init__(timeout=120)
        self.pages = pages
        self.owner_id = owner_id
        self.index = 0
        self._sync_buttons()

    def content(self) -> str:
        """Return the current page with its footer."""
        return f"{self.pages[self.index]}\n\nPage {self.index + 1}/{len(self.pages)}"

    def _sync_buttons(self) -> None:
        self.previous_page.disabled = self.index == 0
        self.next_page.disabled = self.index == len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only the user who asked for help may flip its pages."""
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("This help menu isn't yours.", ephemeral=True)
        return False

    async def _show(self, interaction: discord.Interaction, index: int) -> None:
        self.index = index
        self._sync_buttons()
        await interaction.response.edit_message(content=self.content(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button[HelpPaginator]
    ) -> None:
        """Go back one page."""
        await self._show(interaction, self.index - 1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button[HelpPaginator]
    ) -> None:
        """Go forward one page."""
        await self._show(interaction, self.index + 1)


class Help(commands.Cog):
    """Lists available commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @hybrid_command(name="help", description="List available commands.")
    async def help(self, ctx: commands.Context[commands.Bot]) -> None:
        """Reply privately with every visible command and its description."""
        lines = [
            f"`/{command.qualified_name}` — {command.description or 'No description'}"
            for command in self.bot.walk_commands()
            if not command.hidden
        ]
        pages = paginate(lines, PAGE_LIMIT)
        if not pages:
            await ctx.reply("No commands available.", ephemeral=True)
        elif len(pages) == 1:
            await ctx.reply(pages[0], ephemeral=True)
        else:
            view = HelpPaginator(pages, ctx.author.id)
            await ctx.reply(view.content(), view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Help(bot))
