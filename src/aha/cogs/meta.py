"""Diagnostics and meta commands."""

from __future__ import annotations

from discord.ext import commands


class Meta(commands.Cog):
    """Basic bot introspection commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # NOTE: discord.py's ``HybridCommand`` decorator does not infer its
    # ParamSpec/CogT generics cleanly under strict mypy when applied to a Cog
    # method (the call is valid at runtime). See the discord.py typing gap.
    @commands.hybrid_command(  # type: ignore[arg-type]
        name="ping",
        description="Check the bot's WebSocket latency.",
    )
    async def ping(self, ctx: commands.Context[commands.Bot]) -> None:
        """Reply with the current gateway latency in milliseconds."""
        latency_ms = round(self.bot.latency * 1000)
        await ctx.reply(f"Pong! WebSocket latency: {latency_ms} ms")


async def setup(bot: commands.Bot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Meta(bot))
