"""Paginated help command listing the bot's commands."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import discord
from discord.ext import commands

from aha import logctx
from aha.commands import hybrid_command
from aha.replies import edit_message, reply, respond

__all__ = [
    "CATEGORY_ORDER",
    "DEFAULT_CATEGORY",
    "Help",
    "HelpPaginator",
    "category_of",
    "group_by_category",
    "paginate",
    "paginate_sections",
    "setup",
]

#: Max characters of command text per page. Discord caps a message at 2000; the
#: remainder is headroom for the "Page x/y" footer.
PAGE_LIMIT = 1900


#: Category of a command that does not declare one in ``extras``.
DEFAULT_CATEGORY = "Other"

#: Categories listed first, in this order; any other category follows alphabetically and
#: :data:`DEFAULT_CATEGORY` comes last.
CATEGORY_ORDER = ("Info", "Fun", "Admin")


def category_of(command: Any) -> str:
    """Return the help category a command declares via ``extras={"category": ...}``."""
    declared = (getattr(command, "extras", None) or {}).get("category")
    return str(declared) if declared else DEFAULT_CATEGORY


def _category_rank(category: str) -> tuple[int, int, str]:
    if category in CATEGORY_ORDER:
        return (0, CATEGORY_ORDER.index(category), category)
    if category == DEFAULT_CATEGORY:
        return (2, 0, category)
    return (1, 0, category)


def group_by_category(commands_: Iterable[Any]) -> list[tuple[str, list[str]]]:
    """Group ``/name — description`` lines by category, in display order.

    Commands keep their registration order inside a category.
    """
    grouped: dict[str, list[str]] = {}
    for command in commands_:
        line = f"`/{command.qualified_name}` — {command.description or 'No description'}"
        grouped.setdefault(category_of(command), []).append(line)
    return [(name, grouped[name]) for name in sorted(grouped, key=_category_rank)]


def paginate_sections(
    sections: Sequence[tuple[str, list[str]]], limit: int = PAGE_LIMIT
) -> list[str]:
    """Pack titled sections into pages of at most ``limit`` characters.

    A section header is never left alone at the bottom of a page (it travels with the section's
    next line), and a section that continues on a new page repeats its header marked
    ``(continued)``. With a single section the header is omitted and this is :func:`paginate`.
    """
    if len(sections) <= 1:
        return paginate([line for _, lines in sections for line in lines], limit)
    pages: list[str] = []
    current: list[str] = []
    for name, lines in sections:
        header = f"**{name}**"
        # A line must fit beside the longest header it can be paired with.
        room = max(limit - len(f"{header} (continued)") - 1, 1)
        started = False  # whether this section already has a line on an earlier page
        pending = [header]
        for line in lines:
            if len(line) > room:
                line = line[: room - 1] + "…"
            if current and len("\n".join([*current, *pending, line])) > limit:
                pages.append("\n".join(current))
                current = []
                pending = [f"**{name}** (continued)"] if started else [header]
            current.extend([*pending, line])
            pending = []
            started = True
    if current:
        pages.append("\n".join(current))
    return pages


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
        # Button clicks never pass the command tree's check, so bind the log context here.
        logctx.bind_interaction(interaction)
        if interaction.user.id == self.owner_id:
            return True
        await respond(interaction, "This help menu isn't yours.", ephemeral=True)
        return False

    async def _show(self, interaction: discord.Interaction, index: int) -> None:
        self.index = index
        self._sync_buttons()
        await edit_message(interaction, self.content(), view=self)

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

    @hybrid_command(
        name="help", description="List available commands.", extras={"category": "Info"}
    )
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def help(self, ctx: commands.Context[commands.Bot]) -> None:
        """Reply privately with every visible command and its description."""
        visible = [command for command in self.bot.walk_commands() if not command.hidden]
        pages = paginate_sections(group_by_category(visible), PAGE_LIMIT)
        if not pages:
            await reply(ctx, "No commands available.", ephemeral=True)
        elif len(pages) == 1:
            await reply(ctx, pages[0], ephemeral=True)
        else:
            view = HelpPaginator(pages, ctx.author.id)
            await reply(ctx, view.content(), view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Help(bot))
