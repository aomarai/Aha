"""Diagnostics and meta commands."""

from __future__ import annotations

import discord
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.botinfo import build_info, format_latency
from aha.commands import hybrid_command
from aha.replies import reply

__all__ = ["PRIVACY_URL", "TERMS_URL", "Meta", "setup"]

#: Where the policy and terms live until they are published somewhere more formal.
PRIVACY_URL = "https://github.com/aomarai/Aha/blob/main/docs/privacy.md"
TERMS_URL = "https://github.com/aomarai/Aha/blob/main/docs/terms.md"


class Meta(commands.Cog):
    """Basic bot introspection."""

    def __init__(self, bot: AhaBot) -> None:
        self.bot = bot

    @hybrid_command(
        name="ping", description="Check the bot's WebSocket latency.", extras={"category": "Info"}
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def ping(self, ctx: commands.Context[commands.Bot]) -> None:
        """Reply with the current gateway latency in milliseconds."""
        text = f"WebSocket latency: {format_latency(self.bot.latency)}"
        try:
            await reply(ctx, embed=embeds.status(text))
        except discord.HTTPException:
            # The channel may lack embed permission; keep the reply alive.
            await reply(ctx, text)

    @hybrid_command(
        name="about",
        description="Show the bot's version, uptime and status.",
        extras={"category": "Info"},
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def about(self, ctx: commands.Context[commands.Bot]) -> None:
        """Reply with the running version, commit, uptime and this process's shards and guilds.

        Static metadata only: no secrets, no guild names, no user data. The guild count covers
        the shards of *this* process; a fleet-wide total needs shared state (#84).
        """
        info = build_info()
        shards = ", ".join(str(state.shard_id) for state in self.bot.shard_states())
        fields = [
            ("Version", f"v{info.version}"),
            ("Commit", info.short_commit),
            ("Started", f"<t:{int(self.bot.started_at)}:R>"),
            ("Guilds (this process)", str(len(self.bot.guilds))),
            ("Shards (this process)", shards or "none"),
            ("Latency", format_latency(self.bot.latency)),
            ("Runtime", f"Python {info.python}, discord.py {info.discord_py}"),
        ]
        try:
            await reply(
                ctx,
                embed=embeds.info(
                    "Source: https://github.com/aomarai/Aha",
                    title="About Aha",
                    fields=[(name, value, True) for name, value in fields],
                ),
            )
        except discord.HTTPException:
            # The channel may lack embed permission; keep the reply alive.
            await reply(ctx, "\n".join(f"{name}: {value}" for name, value in fields))

    @hybrid_command(
        name="privacy",
        description="Show what the bot stores and where the policy is.",
        extras={"category": "Info"},
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def privacy(self, ctx: commands.Context[commands.Bot]) -> None:
        """Summarise the privacy policy and link to it (private to the invoker)."""
        text = (
            "Aha does **not** store your messages or the text of your commands. It keeps only "
            "what you ask it to: reminders you set (until delivered or cancelled) and, for server "
            "settings, the id of the admin who last changed them. `/forgetme` deletes it. "
            "Operational logs record who ran which command (IDs and option *names*, never "
            "values).\n\n"
            f"Read the full policy: <{PRIVACY_URL}>\nTerms: <{TERMS_URL}>"
        )
        try:
            await reply(ctx, embed=embeds.info(text, title="Privacy"), ephemeral=True)
        except discord.HTTPException:
            await reply(ctx, text, ephemeral=True)


async def setup(bot: AhaBot) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Meta(bot))
