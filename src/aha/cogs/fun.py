"""Fun commands: dice, coin, the time somewhere, a magic 8-ball, and countdowns."""

from __future__ import annotations

import random
import zoneinfo
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.commands import BotLike, hybrid_command
from aha.i18n import locale_of, t
from aha.replies import reply
from aha.textutil import code, plain

__all__ = ["ANSWERS", "MAX_SIDES", "Fun", "format_offset", "setup", "timezone_names"]

#: Longest ``/8ball`` question echoed back.
_QUESTION_LIMIT = 200

#: Most sides ``/roll`` accepts; Discord enforces it in the slash UI, the message surface checks it.
MAX_SIDES = 1_000_000

#: Longest ``/countdown`` date or label accepted.
_INPUT_LIMIT = 100

#: Discord shows at most this many autocomplete choices.
_AUTOCOMPLETE_LIMIT = 25

#: The classic Magic 8-Ball answers: ten positive, five unsure, five negative.
ANSWERS = (
    "It is certain.",
    "It is decidedly so.",
    "Without a doubt.",
    "Yes, definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Yes.",
    "Signs point to yes.",
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
)


def utcnow() -> datetime:
    """Return the current time in UTC (a seam for tests)."""
    return datetime.now(UTC)


def timezone_names() -> dict[str, str]:
    """Map lower-cased IANA zone names to their canonical spelling."""
    return {name.lower(): name for name in sorted(zoneinfo.available_timezones())}


def format_offset(offset: timedelta | None) -> str:
    """Format a UTC offset as ``+01:00`` (``+00:00`` when there is none)."""
    total = int((offset or timedelta()).total_seconds())
    hours, minutes = divmod(abs(total) // 60, 60)
    return f"{'-' if total < 0 else '+'}{hours:02d}:{minutes:02d}"


class Fun(commands.Cog):
    """Dice, coin, time, 8-ball and countdown commands."""

    def __init__(self, bot: BotLike) -> None:
        self.bot = bot
        self._zones = timezone_names()

    @hybrid_command(name="roll", description="Roll a die.", extras={"category": "Fun"})
    @app_commands.describe(sides=f"How many sides the die has (1 to {MAX_SIDES:,}, default 6).")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def roll(
        self, ctx: commands.Context[commands.Bot], sides: app_commands.Range[int, 1, MAX_SIDES] = 6
    ) -> None:
        """Roll a die with the given number of sides (default d6)."""
        if sides < 1:
            await reply(ctx, embed=embeds.error("`sides` must be at least 1."), ephemeral=True)
            return
        result = random.randint(1, sides)
        text = f"Rolled **{result}** on a d{sides}."
        try:
            await reply(ctx, embed=embeds.info(text))
        except discord.HTTPException:
            await reply(ctx, text)

    @hybrid_command(name="coin", description="Flip a coin.", extras={"category": "Fun"})
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def coin(self, ctx: commands.Context[commands.Bot]) -> None:
        """Flip a coin and report heads or tails."""
        locale = locale_of(ctx)
        side = t(f"coin.{random.choice(('heads', 'tails'))}", locale)
        text = t("coin.result", locale, side=side)
        try:
            await reply(ctx, embed=embeds.info(text))
        except discord.HTTPException:
            await reply(ctx, text)

    @hybrid_command(
        name="time", description="Show the current time in a timezone.", extras={"category": "Fun"}
    )
    @app_commands.describe(tz="An IANA timezone such as Europe/Berlin (default UTC).")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def time_in(self, ctx: commands.Context[commands.Bot], tz: str = "UTC") -> None:
        """Show the current time in ``tz`` (default UTC)."""
        name = self._zones.get(tz.strip().lower())
        if name is None:
            hint = f"`{code(tz.strip())[:40]}` is not a timezone I know. Try `Europe/Berlin`."
            await reply(ctx, embed=embeds.error(hint), ephemeral=True)
            return
        now = utcnow().astimezone(zoneinfo.ZoneInfo(name))
        text = (
            f"**{name}**\n{now:%A %d %B %Y, %H:%M:%S} "
            f"{now.tzname()} (UTC{format_offset(now.utcoffset())})"
        )
        try:
            await reply(ctx, embed=embeds.info(text))
        except discord.HTTPException:
            await reply(ctx, text)

    @time_in.autocomplete("tz")
    async def time_zone_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Offer timezones whose name contains what the user has typed."""
        needle = current.strip().lower()
        matches = [name for key, name in self._zones.items() if needle in key]
        return [
            app_commands.Choice(name=name, value=name) for name in matches[:_AUTOCOMPLETE_LIMIT]
        ]

    @hybrid_command(
        name="8ball", description="Ask the magic 8-ball a question.", extras={"category": "Fun"}
    )
    @app_commands.describe(question="What you want to know.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def eight_ball(self, ctx: commands.Context[commands.Bot], *, question: str) -> None:
        """Answer ``question`` with a random magic 8-ball reply."""
        shown = plain(question.strip())
        if len(shown) > _QUESTION_LIMIT:
            shown = shown[: _QUESTION_LIMIT - 1] + "…"
        text = f"> {shown}\n🎱 {random.choice(ANSWERS)}"
        try:
            await reply(ctx, embed=embeds.info(text))
        except discord.HTTPException:
            await reply(ctx, text)

    @hybrid_command(
        name="countdown", description="Count down to a date.", extras={"category": "Fun"}
    )
    @app_commands.describe(
        when="A date or time like 2026-12-25 or 2026-12-25 18:00 (UTC unless it has an offset).",
        label="What you are counting down to.",
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def countdown(
        self, ctx: commands.Context[commands.Bot], when: str, *, label: str = ""
    ) -> None:
        """Show how long until ``when``, in every viewer's own timezone."""
        target = _parse_when(when)
        if target is None:
            hint = (
                "I couldn't read that date. Use `2026-12-25` or `2026-12-25 18:00`, "
                "optionally with an offset like `+01:00` (otherwise UTC)."
            )
            await reply(ctx, embed=embeds.error(hint), ephemeral=True)
            return
        if target <= utcnow():
            await reply(ctx, embed=embeds.error("That time has already passed."), ephemeral=True)
            return
        stamp = int(target.timestamp())
        title = plain(label.strip())[:_INPUT_LIMIT] or "Countdown"
        text = f"**{title}**: <t:{stamp}:R> (<t:{stamp}:F>)"
        try:
            await reply(ctx, embed=embeds.info(text))
        except discord.HTTPException:
            await reply(ctx, text)


def _parse_when(raw: str) -> datetime | None:
    """Parse an ISO date or datetime (UTC when it has no offset); ``None`` if unreadable."""
    text = raw.strip()
    if not text or len(text) > _INPUT_LIMIT:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def setup(bot: BotLike) -> None:
    """Register the cog with the bot (called by ``load_extension``)."""
    await bot.add_cog(Fun(bot))
