"""Fun commands: dice and coin."""

from __future__ import annotations

import random

from discord.ext import commands

from aha import embeds
from aha.commands import hybrid_command

__all__ = ["Fun", "setup"]


class Fun(commands.Cog):
    """Dice and coin commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @hybrid_command(name="roll", description="Roll a die.")
    async def roll(self, ctx: commands.Context[commands.Bot], sides: int = 6) -> None:
        """Roll a die with the given number of sides (default d6)."""
        result = random.randint(1, sides)
        await ctx.reply(embed=embeds.info(f"Rolled **{result}** on a d{sides}."))

    @hybrid_command(name="coin", description="Flip a coin.")
    async def coin(self, ctx: commands.Context[commands.Bot]) -> None:
        """Flip a coin and report heads or tails."""
        result = random.choice(("heads", "tails"))
        await ctx.reply(embed=embeds.info(f"{result.title()}!"))


async def setup(bot: commands.Bot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Fun(bot))
