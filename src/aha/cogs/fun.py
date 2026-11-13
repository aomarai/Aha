"""Fun commands: dice and coin."""

from __future__ import annotations

import random

import discord
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
        if sides < 1:
            await ctx.reply("sides must be at least 1.")
            return
        result = random.randint(1, sides)
        text = f"Rolled **{result}** on a d{sides}."
        try:
            await ctx.reply(embed=embeds.info(text))
        except discord.HTTPException:
            await ctx.reply(text)

    @hybrid_command(name="coin", description="Flip a coin.")
    async def coin(self, ctx: commands.Context[commands.Bot]) -> None:
        """Flip a coin and report heads or tails."""
        result = random.choice(("heads", "tails"))
        text = f"{result.title()}!"
        try:
            await ctx.reply(embed=embeds.info(text))
        except discord.HTTPException:
            await ctx.reply(text)


async def setup(bot: commands.Bot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Fun(bot))
