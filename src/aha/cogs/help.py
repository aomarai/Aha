"""Paginated help command listing the bot's commands."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any, ClassVar

import discord
from discord.ext import commands

from aha.commands import BotLike, hybrid_command
from aha.components import require_invoker
from aha.replies import edit_message, reply

__all__ = [
    "CATEGORY_ORDER",
    "DEFAULT_CATEGORY",
    "Help",
    "HelpButton",
    "build_pages",
    "category_of",
    "group_by_category",
    "help_view",
    "page_content",
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


def group_by_category(commands_: Iterable[Any]) -> list[tuple[str, list[str]]]:
    """Group ``/name — description`` lines by category, in display order.

    Known categories come first (in :data:`CATEGORY_ORDER`), then any others alphabetically, then
    :data:`DEFAULT_CATEGORY`. Commands keep their registration order inside a category.
    """
    grouped: dict[str, list[str]] = {}
    for command in commands_:
        line = f"`/{command.qualified_name}` — {command.description or 'No description'}"
        grouped.setdefault(category_of(command), []).append(line)
    known = [name for name in CATEGORY_ORDER if name in grouped]
    others = sorted(name for name in grouped if name not in (*CATEGORY_ORDER, DEFAULT_CATEGORY))
    last = [DEFAULT_CATEGORY] if DEFAULT_CATEGORY in grouped else []
    return [(name, grouped[name]) for name in (*known, *others, *last)]


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
        continued = f"{header} (continued)"
        # A line must fit beside the longest header it can be paired with.
        room = limit - len(continued) - 1
        pending = [header]
        for position, line in enumerate(lines):
            if len(line) > room:
                line = line[: max(room, 1) - 1] + "…"
            if current and len("\n".join([*current, *pending, line])) > limit:
                pages.append("\n".join(current))
                current = []
                pending = [header if position == 0 else continued]
            current.extend([*pending, line])
            pending = []
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


def build_pages(bot: BotLike) -> list[str]:
    """Build the help pages from the bot's current commands."""
    visible = [command for command in bot.walk_commands() if not command.hidden]
    return paginate_sections(group_by_category(visible), PAGE_LIMIT)


def page_content(pages: Sequence[str], index: int) -> str:
    """Return page ``index`` with its "Page x/y" footer."""
    return f"{pages[index]}\n\nPage {index + 1}/{len(pages)}"


def help_view(index: int, total: int, owner_id: int) -> discord.ui.View:
    """Build the Previous/Next buttons for page ``index`` of ``total``.

    Each button's ``custom_id`` carries the page it leads to and the owner, so a click is handled
    correctly after a restart and by any process: nothing is remembered between clicks.
    """
    view = discord.ui.View(timeout=None)
    view.add_item(HelpButton("prev", max(index - 1, 0), owner_id, disabled=index <= 0))
    view.add_item(
        HelpButton("next", min(index + 1, total - 1), owner_id, disabled=index >= total - 1)
    )
    return view


class HelpButton(
    discord.ui.DynamicItem[discord.ui.Button[Any]],
    template=r"aha:help:(?P<direction>prev|next):(?P<page>\d+):(?P<owner>\d+)",
):
    """A Previous/Next button whose whole state is its ``custom_id``.

    ``aha:help:<prev|next>:<page to show>:<owner id>``. The pages are rebuilt from the live
    command list on every click, so the menu stays correct across restarts and rollouts (a page
    number past the end, after commands were removed, is clamped).
    """

    _LABELS: ClassVar[dict[str, str]] = {"prev": "Previous", "next": "Next"}
    _STYLES: ClassVar[dict[str, discord.ButtonStyle]] = {
        "prev": discord.ButtonStyle.secondary,
        "next": discord.ButtonStyle.primary,
    }

    def __init__(self, direction: str, page: int, owner_id: int, *, disabled: bool = False) -> None:
        super().__init__(
            discord.ui.Button(
                label=self._LABELS[direction],
                style=self._STYLES[direction],
                custom_id=f"aha:help:{direction}:{page}:{owner_id}",
                disabled=disabled,
            )
        )
        self.page = page
        self.owner_id = owner_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction[Any],
        item: discord.ui.Item[Any],
        match: re.Match[str],
        /,
    ) -> HelpButton:
        """Rebuild the button from a ``custom_id`` that matched the template."""
        return cls(match["direction"], int(match["page"]), int(match["owner"]))

    async def interaction_check(self, interaction: discord.Interaction[Any]) -> bool:
        """Only the user who asked for help may flip its pages."""
        return await require_invoker(
            interaction, self.owner_id, message="This help menu isn't yours."
        )

    async def callback(self, interaction: discord.Interaction[Any]) -> None:
        """Show the page this button leads to."""
        pages = build_pages(interaction.client)
        if not pages:
            await edit_message(interaction, "No commands available.", view=None)
            return
        index = min(self.page, len(pages) - 1)
        await edit_message(
            interaction,
            page_content(pages, index),
            view=help_view(index, len(pages), self.owner_id),
        )


class Help(commands.Cog):
    """Lists available commands."""

    def __init__(self, bot: BotLike) -> None:
        self.bot = bot

    @hybrid_command(
        name="help", description="List available commands.", extras={"category": "Info"}
    )
    @commands.cooldown(3, 10, commands.BucketType.user)
    async def help(self, ctx: commands.Context[commands.Bot]) -> None:
        """Reply privately with every visible command and its description."""
        pages = build_pages(self.bot)
        if not pages:
            await reply(ctx, "No commands available.", ephemeral=True)
        elif len(pages) == 1:
            await reply(ctx, pages[0], ephemeral=True)
        else:
            await reply(
                ctx,
                page_content(pages, 0),
                view=help_view(0, len(pages), ctx.author.id),
                ephemeral=True,
            )


async def setup(bot: BotLike) -> None:
    """Register the cog and its persistent button with the bot (called by ``load_extension``)."""
    bot.add_dynamic_items(HelpButton)
    await bot.add_cog(Help(bot))
