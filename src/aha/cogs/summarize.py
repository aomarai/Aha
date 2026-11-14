"""``/summarize`` and the "Summarize" message menu: a short summary from a Claude model (#110).

Off in every server until an admin runs ``/configure summaries on``, and unavailable entirely
unless the operator set ``ANTHROPIC_API_KEY``. Because text leaves Aha for a third party, the
controls are strict: a per-user daily quota (a refund if the model call fails), input capped at
``SUMMARIZE_MAX_CHARS``, a bounded answer, a 20 s timeout after an immediate ``defer``, the user's
text sent as data, and the answer shown as plain text without mentions. Only counters are stored,
never the text (``summarize_usage``, deleted by ``/forgetme``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.i18n import locale_of, t
from aha.replies import reply
from aha.summarize_store import UsageStore
from aha.summarizer import AnthropicSummarizer, SummarizeError, Summarizer
from aha.textutil import clip, plain

__all__ = ["EMBED_LIMIT", "Outcome", "Summarize", "setup"]

log = logging.getLogger(__name__)

#: Discord's embed description limit.
EMBED_LIMIT = 4000


@dataclass(frozen=True, slots=True)
class Outcome:
    """What to tell the user, and whether it is the summary itself."""

    text: str
    ok: bool


def _embed(outcome: Outcome, locale: str) -> discord.Embed:
    """A summary as an info card, anything else as an error card."""
    if outcome.ok:
        return embeds.info(outcome.text, title=t("summarize.title", locale))
    return embeds.error(outcome.text)


class Summarize(commands.Cog):
    """The summarize command and its message menu."""

    def __init__(
        self,
        bot: AhaBot,
        summarizer: Summarizer | None = None,
        usage: UsageStore | None = None,
    ) -> None:
        self.bot = bot
        self._summarizer = summarizer
        self._usage = usage if usage is not None else (UsageStore(bot.db) if bot.db else None)

        # See Polls: a thin nested callback keeps this module's globals for discord.py's
        # annotation resolution, including under the mutation-testing wrapper.
        async def menu(interaction: discord.Interaction, message: discord.Message) -> None:
            await self.summarize_message(interaction, message)

        self._menu = app_commands.ContextMenu(name="Summarize", callback=menu)

    async def cog_load(self) -> None:
        """Register the message context menu."""
        self.bot.tree.add_command(self._menu)

    async def cog_unload(self) -> None:
        """Remove the context menu again."""
        self.bot.tree.remove_command(self._menu.name, type=self._menu.type)

    async def run(self, *, user_id: int, guild_id: int | None, locale: str, text: str) -> Outcome:
        """Apply every control, call the model, and say what happened (never raises)."""
        metrics = self.bot.metrics
        settings = self.bot.settings
        if self._summarizer is None or self._usage is None or self.bot.guild_settings is None:
            metrics.summaries.labels(outcome="disabled").inc()
            return Outcome(t("summarize.unavailable", locale), False)
        if guild_id is None:
            metrics.summaries.labels(outcome="disabled").inc()
            return Outcome(t("summarize.guild_only", locale), False)
        guild = await self.bot.guild_settings.get(guild_id)
        if not guild.summaries_enabled:
            metrics.summaries.labels(outcome="disabled").inc()
            return Outcome(t("summarize.disabled", locale), False)
        body = text.strip()
        if not body:
            metrics.summaries.labels(outcome="empty").inc()
            return Outcome(t("summarize.empty", locale), False)
        body = body[: settings.summarize_max_chars]
        if await self._usage.consume(user_id, settings.summarize_daily_quota) is None:
            metrics.summaries.labels(outcome="quota").inc()
            quota = settings.summarize_daily_quota
            return Outcome(t("summarize.quota", locale, quota=quota), False)
        try:
            summary = await self._summarizer.summarize(body)
        except SummarizeError as error:
            await self._usage.refund(user_id)
            log.warning("Summary failed: %s", error)
            metrics.summaries.labels(outcome="failed").inc()
            return Outcome(t("summarize.failed", locale), False)
        metrics.summaries.labels(outcome="ok").inc()
        metrics.summary_tokens.labels(kind="input").inc(summary.input_tokens)
        metrics.summary_tokens.labels(kind="output").inc(summary.output_tokens)
        return Outcome(clip(plain(summary.text), EMBED_LIMIT), True)

    @hybrid_command(
        name="summarize",
        description="Summarize a piece of text (off until a server admin enables it).",
        extras={"category": "Fun"},
    )
    @app_commands.describe(text="The text to summarize.")
    @commands.guild_only()
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def summarize(self, ctx: commands.Context[commands.Bot], *, text: str) -> None:
        """Summarize ``text`` privately for the invoker."""
        await ctx.defer(ephemeral=True)
        outcome = await self.run(
            user_id=ctx.author.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            locale=locale_of(ctx),
            text=text,
        )
        await reply(ctx, embed=_embed(outcome, locale_of(ctx)), ephemeral=True)

    async def summarize_message(
        self, interaction: discord.Interaction, message: discord.Message
    ) -> None:
        """The menu: summarize a message's text, privately. The menu delivers its content."""
        await interaction.response.defer(ephemeral=True)
        locale = locale_of(interaction)
        outcome = await self.run(
            user_id=interaction.user.id,
            guild_id=interaction.guild_id,
            locale=locale,
            text=message.content,
        )
        await interaction.followup.send(
            embed=_embed(outcome, locale),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


async def setup(bot: AhaBot) -> None:
    """Add the cog; the model client exists only when an API key is configured."""
    key = bot.settings.anthropic_api_key
    summarizer = AnthropicSummarizer(key, bot.settings.anthropic_model) if key else None
    await bot.add_cog(Summarize(bot, summarizer))
