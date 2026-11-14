"""Durable reminders: ``/remind``, ``/reminders`` and ``/unremind``.

Reminders are rows in ``scheduled_jobs`` (kind ``reminder``), so they survive a restart and fire
once whichever pod runs the scheduler (ADR 0001, #96). They need ``DATABASE_URL``; without it the
commands say so. Delivery is at-least-once, so the message carries a deterministic nonce.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.durations import DurationError, parse_duration
from aha.i18n import locale_of, t
from aha.jobstore import Job, JobStore
from aha.replies import reply
from aha.textutil import plain

__all__ = ["KIND", "MAX_ACTIVE", "MAX_HORIZON", "TEXT_LIMIT", "Reminders", "setup"]

log = logging.getLogger(__name__)

#: ``scheduled_jobs.kind`` for reminders.
KIND = "reminder"

#: Most live reminders one user may hold.
MAX_ACTIVE = 25

#: Furthest into the future a reminder may be set.
MAX_HORIZON = timedelta(days=365)

#: Longest reminder text stored.
TEXT_LIMIT = 1000

#: Longest preview of a reminder in a list or autocomplete choice.
_PREVIEW = 60

#: Discord shows at most this many autocomplete choices.
_AUTOCOMPLETE_LIMIT = 25


def utcnow() -> datetime:
    """Return the current time in UTC (a seam for tests)."""
    return datetime.now(UTC)


def _preview(text: str) -> str:
    """Shorten ``text`` to one line for lists."""
    flat = " ".join(text.split())
    return flat if len(flat) <= _PREVIEW else flat[: _PREVIEW - 1] + "…"


class Reminders(commands.Cog):
    """Set, list, cancel and deliver reminders."""

    def __init__(self, bot: AhaBot, store: JobStore | None = None) -> None:
        self.bot = bot
        self._store = store if store is not None else (JobStore(bot.db) if bot.db else None)

    async def _require_store(self, ctx: commands.Context[commands.Bot]) -> JobStore | None:
        """Return the store, or tell the user reminders are off in this deployment."""
        if self._store is None:
            await reply(
                ctx, embed=embeds.error(t("remind.unavailable", locale_of(ctx))), ephemeral=True
            )
        return self._store

    @hybrid_command(
        name="remind", description="Remind you of something later.", extras={"category": "Info"}
    )
    @app_commands.describe(when="How long from now, like 30m, 2h or 1d 12h.", what="What to say.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def remind(self, ctx: commands.Context[commands.Bot], when: str, *, what: str) -> None:
        """Remind the invoker of ``what`` after ``when``."""
        store = await self._require_store(ctx)
        if store is None:
            return
        locale = locale_of(ctx)
        try:
            delay = parse_duration(when)
        except DurationError as error:
            await reply(ctx, embed=embeds.error(t(error.key, locale)), ephemeral=True)
            return
        if delay > MAX_HORIZON:
            message = t("remind.too_far", locale, days=MAX_HORIZON.days)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        text = what.strip()
        if not text or len(text) > TEXT_LIMIT:
            message = t("remind.bad_text", locale, limit=TEXT_LIMIT)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        if await store.count(KIND, ctx.author.id) >= MAX_ACTIVE:
            message = t("remind.too_many", locale, limit=MAX_ACTIVE)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        run_at = utcnow() + delay
        job_id = await store.enqueue(
            KIND,
            run_at,
            user_id=ctx.author.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            payload={"channel_id": ctx.channel.id, "text": text},
        )
        when_text = discord.utils.format_dt(run_at, "R")
        message = t("remind.created", locale, when=when_text, id=job_id)
        await reply(ctx, embed=embeds.success(message), ephemeral=True)

    @hybrid_command(
        name="reminders", description="List your reminders.", extras={"category": "Info"}
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def reminders(self, ctx: commands.Context[commands.Bot]) -> None:
        """List the invoker's own reminders, soonest first."""
        store = await self._require_store(ctx)
        if store is None:
            return
        jobs = await store.list_for_user(KIND, ctx.author.id, MAX_ACTIVE)
        if not jobs:
            message = t("remind.none", locale_of(ctx))
            await reply(ctx, embed=embeds.info(message), ephemeral=True)
            return
        lines = [
            f"`{job.id}` {discord.utils.format_dt(job.run_at, 'R')}: "
            f"{plain(_preview(str(job.payload.get('text', ''))))}"
            for job in jobs
        ]
        await reply(ctx, embed=embeds.info("\n".join(lines)), ephemeral=True)

    @hybrid_command(
        name="unremind", description="Cancel one of your reminders.", extras={"category": "Info"}
    )
    @app_commands.describe(reminder="The reminder to cancel.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def unremind(self, ctx: commands.Context[commands.Bot], reminder: int) -> None:
        """Cancel one of the invoker's own reminders (never anyone else's)."""
        store = await self._require_store(ctx)
        if store is None:
            return
        locale = locale_of(ctx)
        if await store.cancel(KIND, ctx.author.id, reminder):
            message = t("remind.cancelled", locale)
            await reply(ctx, embed=embeds.success(message), ephemeral=True)
        else:
            message = t("remind.not_found", locale)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)

    @unremind.autocomplete("reminder")
    async def reminder_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """Offer the caller's own reminders whose text contains what they typed."""
        if self._store is None:
            return []
        needle = current.strip().lower()
        jobs = await self._store.list_for_user(KIND, interaction.user.id, MAX_ACTIVE)
        return [
            app_commands.Choice(name=f"{job.id}: {_preview(text)}", value=job.id)
            for job in jobs
            if needle in (text := str(job.payload.get("text", ""))).lower()
        ][:_AUTOCOMPLETE_LIMIT]

    async def deliver(self, job: Job) -> None:
        """Send a due reminder: in its channel, else by DM, else drop it.

        Only the invoker can be pinged. A missing channel or permission falls back to a DM; closed
        DMs drop the reminder (logged). Any other Discord error propagates so the scheduler
        retries. The nonce makes a redelivery after a crash a no-op where Discord can tell.
        """
        user_id = job.user_id
        if user_id is None:
            return
        text = f"<@{user_id}> ⏰ {plain(str(job.payload.get('text', '')))}"
        mentions = discord.AllowedMentions(
            everyone=False, roles=False, users=[discord.Object(id=user_id)]
        )
        send: dict[str, Any] = {"allowed_mentions": mentions, "nonce": job.id}
        channel_id = job.payload.get("channel_id")
        channel = self.bot.get_channel(channel_id) if isinstance(channel_id, int) else None
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(text, **send)
                return
            except discord.Forbidden, discord.NotFound:
                log.info("Reminder %s: channel unavailable, falling back to a DM", job.id)
        try:
            user = await self.bot.fetch_user(user_id)
            await user.send(text, **send)
        except discord.Forbidden, discord.NotFound:
            log.warning("Reminder %s dropped: the channel and the DM are both unreachable", job.id)


async def setup(bot: AhaBot) -> None:
    """Add the cog and register its job handler when a scheduler exists."""
    cog = Reminders(bot)
    await bot.add_cog(cog)
    if bot.scheduler is not None:
        bot.scheduler.register(KIND, cog.deliver)
