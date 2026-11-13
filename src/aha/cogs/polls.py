"""Polls and reaction counts.

``/poll`` creates one of Discord's **native polls**: Discord stores the votes, enforces one vote
per person (or several, if allowed), shows the results and closes the poll on time. The bot keeps
no state and needs no extra intent, so a poll behaves the same whichever process handled the
command and survives restarts. "Count reactions" is a message context menu, which receives the
message without the privileged Message Content intent.
"""

from __future__ import annotations

from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.commands import hybrid_command
from aha.replies import reply, respond

__all__ = [
    "MAX_ANSWER_LENGTH",
    "MAX_DURATION_HOURS",
    "MAX_OPTIONS",
    "MAX_QUESTION_LENGTH",
    "MIN_OPTIONS",
    "Polls",
    "parse_options",
    "setup",
]

#: Discord's poll limits: a 300-character question, 2 to 10 answers of up to 55 characters, and a
#: duration of up to 32 days.
MAX_QUESTION_LENGTH = 300
MIN_OPTIONS = 2
MAX_OPTIONS = 10
MAX_ANSWER_LENGTH = 55
MAX_DURATION_HOURS = 768


def parse_options(raw: str) -> list[str] | str:
    """Split a comma-separated list of poll answers.

    Returns the answers, or a message explaining what is wrong with the input.
    """
    options = [part.strip() for part in raw.split(",") if part.strip()]
    if len(options) < MIN_OPTIONS:
        return f"Give at least {MIN_OPTIONS} options, separated by commas."
    if len(options) > MAX_OPTIONS:
        return f"A poll can have at most {MAX_OPTIONS} options (you gave {len(options)})."
    too_long = next((option for option in options if len(option) > MAX_ANSWER_LENGTH), None)
    if too_long is not None:
        return f"Each option can be at most {MAX_ANSWER_LENGTH} characters."
    if len({option.casefold() for option in options}) != len(options):
        return "Each option must be different."
    return options


class Polls(commands.Cog):
    """Native polls, and a context menu that counts a message's reactions."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._count_menu = app_commands.ContextMenu(
            name="Count reactions", callback=self.count_reactions
        )

    async def cog_load(self) -> None:
        """Register the message context menu with the command tree."""
        self.bot.tree.add_command(self._count_menu)

    async def cog_unload(self) -> None:
        """Remove the context menu again."""
        self.bot.tree.remove_command(self._count_menu.name, type=self._count_menu.type)

    @hybrid_command(
        name="poll", description="Start a poll people can vote in.", extras={"category": "Fun"}
    )
    @app_commands.describe(
        question="What you are asking.",
        options="The answers, separated by commas (2 to 10).",
        duration="Hours the poll stays open (1 to 768, default 24).",
        multi="Let people pick more than one answer.",
    )
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def poll(
        self,
        ctx: commands.Context[commands.Bot],
        question: str,
        options: str,
        duration: app_commands.Range[int, 1, MAX_DURATION_HOURS] = 24,
        multi: bool = False,
    ) -> None:
        """Create a native Discord poll in the channel."""
        asked = question.strip()
        if not asked or len(asked) > MAX_QUESTION_LENGTH:
            problem = f"The question must be 1 to {MAX_QUESTION_LENGTH} characters long."
            await reply(ctx, embed=embeds.error(problem), ephemeral=True)
            return
        answers = parse_options(options)
        if isinstance(answers, str):
            await reply(ctx, embed=embeds.error(answers), ephemeral=True)
            return
        native = discord.Poll(question=asked, duration=timedelta(hours=duration), multiple=multi)
        for answer in answers:
            native.add_answer(text=answer)
        await reply(ctx, poll=native)

    async def count_reactions(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """Tell the user, privately, how many of each reaction a message has."""
        if not message.reactions:
            await respond(interaction, "There are no reactions on that message.", ephemeral=True)
            return
        lines = [f"{reaction.emoji} {reaction.count}" for reaction in message.reactions]
        await respond(interaction, "\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Polls(bot))
