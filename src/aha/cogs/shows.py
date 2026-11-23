"""``/watch``, ``/watching`` and ``/unwatch``: reminders for when a show's next episode comes out.

A subscription follows a show for good: after each reminder the next episode is looked up and
scheduled, so a weekly show reminds weekly until it ends. Schedules come from TVmaze
(:mod:`aha.tvmaze`); the timers are ordinary ``scheduled_jobs`` (kind ``show``), so they survive a
restart and run on whichever pod is free (ADR 0001). Reminders go by DM, or ping the user in the
channel where they subscribed.

Each job is checked before it notifies: the episode is looked up again, and a postponed one is
rescheduled instead of announced early. TVmaze being unreachable never ends a subscription: the
stored schedule is trusted, and a lookup that fails is retried in an hour. Delivery is
at-least-once (see :mod:`aha.scheduler`); a compare-and-swap on the subscription's job id
(:meth:`aha.showstore.ShowStore.advance`) keeps a redelivered job from starting a second chain.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from aha import embeds
from aha.bot import AhaBot
from aha.commands import hybrid_command
from aha.delivery import send_to_user
from aha.durations import DurationError, parse_duration
from aha.i18n import locale_of, t
from aha.jobstore import Job
from aha.replies import reply
from aha.showstore import KIND, Followed, ShowLimitError, ShowStore, Subscription
from aha.textutil import clip, pack, plain
from aha.tvmaze import Episode, Show, TvmazeClient, TvmazeError

__all__ = [
    "MAX_ACTIVE",
    "MAX_LEAD",
    "RECHECK_EMPTY",
    "RECHECK_SOON",
    "REQUEUE_AFTER_ATTEMPTS",
    "SLACK",
    "Shows",
    "setup",
]

log = logging.getLogger(__name__)

#: Most shows one user may follow.
MAX_ACTIVE = 25

#: Longest "remind me this long before" lead.
MAX_LEAD = timedelta(days=7)

#: When no next episode is announced yet, ask again after this long.
RECHECK_EMPTY = timedelta(hours=24)

#: After a failed lookup, or one that gave nothing new, ask again after this long.
RECHECK_SOON = timedelta(hours=1)

#: A reminder that Discord still would not take after this many scheduler attempts is moved an hour
#: later instead of failing again: the scheduler buries a job after five, which would end the
#: subscription silently during a long Discord outage.
REQUEUE_AFTER_ATTEMPTS = 3

#: Longest ``/watching`` page (an embed description holds 4096; a message's embeds 6000 in all).
PAGE_LIMIT = 3800

#: Longest show name and episode title in a list row, so 25 rows fit in two pages.
_ROW_NAME = 60
_ROW_TITLE = 50

#: A rescheduled air time within this much of the planned one is not worth moving the reminder for.
SLACK = timedelta(minutes=5)

#: Autocomplete must answer within Discord's 3 seconds.
AUTOCOMPLETE_TIMEOUT = 2.5

#: Discord shows at most this many autocomplete choices; a choice name is at most 100 characters.
_CHOICES = 25
_NAME_LIMIT = 100

#: The ``where`` option values.
DM = "dm"
CHANNEL = "channel"


def utcnow() -> datetime:
    """Return the current time in UTC (a seam for tests)."""
    return datetime.now(UTC)


def episode_payload(episode: Episode) -> dict[str, Any]:
    """The job payload describing ``episode`` (JSON-safe)."""
    return {
        "ep": {
            "id": episode.id,
            "title": episode.title,
            "season": episode.season,
            "number": episode.number,
            "at": episode.airs_at.isoformat(),
            "approx": episode.approximate,
        }
    }


def episode_from_payload(payload: dict[str, Any]) -> Episode | None:
    """Read the episode back from a job payload; ``None`` when absent or unreadable."""
    raw = payload.get("ep")
    if not isinstance(raw, dict):
        return None
    try:
        return Episode(
            id=int(raw["id"]),
            title=str(raw["title"]),
            season=None if raw["season"] is None else int(raw["season"]),
            number=None if raw["number"] is None else int(raw["number"]),
            airs_at=datetime.fromisoformat(str(raw["at"])),
            approximate=bool(raw["approx"]),
        )
    except KeyError, TypeError, ValueError:
        return None


def format_lead(seconds: int) -> str:
    """``2d``, ``3h`` or ``45m`` for a lead time (the largest unit that divides it evenly)."""
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


class Shows(commands.Cog):
    """Follow shows, list what you follow, unfollow, and deliver the reminders."""

    def __init__(
        self,
        bot: AhaBot,
        store: ShowStore | None = None,
        client: TvmazeClient | None = None,
    ) -> None:
        self.bot = bot
        self._store = store if store is not None else (ShowStore(bot.db) if bot.db else None)
        self._client = client if client is not None else TvmazeClient()

    async def _require_store(self, ctx: commands.Context[commands.Bot]) -> ShowStore | None:
        """Return the store, or tell the user the feature is off in this deployment."""
        if self._store is None:
            message = t("shows.unavailable", locale_of(ctx))
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
        return self._store

    async def _find(self, text: str) -> Show | None:
        """The show a user meant: a TVmaze id from autocomplete, else the best search match."""
        query = text.strip()
        if query.isdecimal():
            found = await self._client.schedule(int(query))
            return None if found is None else found.show
        matches = await self._client.search(query)
        return matches[0] if matches else None

    @hybrid_command(
        name="watch",
        description="Get a reminder when a show's next episode comes out.",
        extras={"category": "Info"},
    )
    @app_commands.describe(
        show="The show to follow (pick one from the list).",
        where="Where to remind you. Direct messages are private.",
        lead="Remind you this long before, like 30m or 1d. Default: when it airs.",
    )
    @app_commands.choices(
        where=[
            app_commands.Choice(name="Direct message", value=DM),
            app_commands.Choice(name="Ping me in this channel", value=CHANNEL),
        ]
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def watch(
        self,
        ctx: commands.Context[commands.Bot],
        show: str,
        where: str = DM,
        lead: str = "",
    ) -> None:
        """Follow ``show``: remind the invoker before every episode until it ends."""
        store = await self._require_store(ctx)
        if store is None:
            return
        await ctx.defer(ephemeral=True)
        locale = locale_of(ctx)
        lead_time = timedelta()
        if lead.strip():
            try:
                lead_time = parse_duration(lead)
            except DurationError as error:
                await reply(ctx, embed=embeds.error(t(error.key, locale)), ephemeral=True)
                return
            if lead_time > MAX_LEAD:
                message = t("shows.lead_too_long", locale, days=MAX_LEAD.days)
                await reply(ctx, embed=embeds.error(message), ephemeral=True)
                return
        if where == CHANNEL and ctx.guild is None:
            message = t("shows.channel_needs_server", locale)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        if await store.count(ctx.author.id) >= MAX_ACTIVE:
            message = t("shows.too_many", locale, limit=MAX_ACTIVE)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        try:
            found = await self._find(show)
            schedule = await self._client.schedule(found.id) if found else None
        except TvmazeError as error:
            log.warning("Show lookup failed: %s", error)
            await reply(ctx, embed=embeds.error(t("shows.lookup_failed", locale)), ephemeral=True)
            return
        if schedule is None:
            message = t("shows.not_found", locale)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        title = plain(schedule.show.name)
        if schedule.show.ended and schedule.next_episode is None:
            message = t("shows.ended", locale, show=title)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        episode = schedule.next_episode
        now = utcnow()
        if episode is None:
            run_at, payload = now + RECHECK_EMPTY, {}
        else:
            run_at, payload = max(now, episode.airs_at - lead_time), episode_payload(episode)
        in_channel = where == CHANNEL and ctx.guild is not None
        try:
            added = await store.add(
                user_id=ctx.author.id,
                guild_id=ctx.guild.id if in_channel and ctx.guild else None,
                channel_id=ctx.channel.id if in_channel else None,
                show_id=schedule.show.id,
                show_name=schedule.show.name,
                lead_seconds=int(lead_time.total_seconds()),
                locale=locale,
                run_at=run_at,
                payload=payload,
                limit=MAX_ACTIVE,
            )
        except ShowLimitError:  # another /watch took the last place between the check and here
            message = t("shows.too_many", locale, limit=MAX_ACTIVE)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        if added is None:
            message = t("shows.already", locale, show=title)
            await reply(ctx, embed=embeds.error(message), ephemeral=True)
            return
        message = t("shows.followed", locale, show=title, details=self._details(schedule.show))
        message += "\n" + (
            t("shows.next", locale, episode=self._label(episode, locale), when=self._when(episode))
            if episode
            else t("shows.no_date", locale)
        )
        message += "\n" + t("shows.credit", locale)
        await reply(ctx, embed=embeds.success(message), ephemeral=True)

    @watch.autocomplete("show")
    async def show_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Offer matching shows (the value is the TVmaze id, so the pick is unambiguous)."""
        if len(current.strip()) < 2:
            return []
        try:
            async with asyncio.timeout(AUTOCOMPLETE_TIMEOUT):
                matches = await self._client.search(current)
        except TvmazeError, TimeoutError:
            return []
        return [
            app_commands.Choice(
                name=clip(
                    f"{match.name} ({match.year})" if match.year else match.name, _NAME_LIMIT
                ),
                value=str(match.id),
            )
            for match in matches
        ][:_CHOICES]

    @hybrid_command(
        name="watching",
        description="List the shows you get reminders for.",
        extras={"category": "Info"},
    )
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def watching(self, ctx: commands.Context[commands.Bot]) -> None:
        """List the invoker's shows with the next episode of each."""
        store = await self._require_store(ctx)
        if store is None:
            return
        locale = locale_of(ctx)
        followed = await store.list_for_user(ctx.author.id, MAX_ACTIVE)
        if not followed:
            await reply(ctx, embed=embeds.info(t("shows.none", locale)), ephemeral=True)
            return
        rows = [self._line(item, locale) for item in followed]
        pages = pack([*rows, f"\n{t('shows.credit', locale)}"], PAGE_LIMIT)
        await reply(ctx, embeds=[embeds.info(page.strip()) for page in pages], ephemeral=True)

    @hybrid_command(
        name="unwatch",
        description="Stop getting reminders for a show.",
        extras={"category": "Info"},
    )
    @app_commands.describe(show="The show to stop following.")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def unwatch(self, ctx: commands.Context[commands.Bot], show: int) -> None:
        """Unfollow one of the invoker's own shows (never anyone else's)."""
        store = await self._require_store(ctx)
        if store is None:
            return
        locale = locale_of(ctx)
        if await store.remove(ctx.author.id, show):
            await reply(ctx, embed=embeds.success(t("shows.unfollowed", locale)), ephemeral=True)
        else:
            await reply(ctx, embed=embeds.error(t("shows.not_following", locale)), ephemeral=True)

    @unwatch.autocomplete("show")
    async def followed_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[int]]:
        """Offer the caller's own shows whose name contains what they typed."""
        if self._store is None:
            return []
        needle = current.strip().lower()
        followed = await self._store.list_for_user(interaction.user.id, MAX_ACTIVE)
        return [
            app_commands.Choice(
                name=clip(item.subscription.show_name, _NAME_LIMIT), value=item.subscription.id
            )
            for item in followed
            if needle in item.subscription.show_name.lower()
        ][:_CHOICES]

    # -- rendering ---------------------------------------------------------------------------

    @staticmethod
    def _details(show: Show) -> str:
        """``Apple TV, 2022``: where and when the show started, whichever is known."""
        parts = [plain(show.network)] if show.network else []
        if show.year:
            parts.append(str(show.year))
        return ", ".join(parts)

    @staticmethod
    def _label(episode: Episode | None, locale: str) -> str:
        """``S2E04 “Title”``, or what is known of it."""
        if episode is None:
            return t("shows.an_episode", locale)
        title = f"“{clip(plain(episode.title), _ROW_TITLE)}”" if episode.title else ""
        return " ".join(part for part in (episode.code, title) if part) or t(
            "shows.an_episode", locale
        )

    @staticmethod
    def _when(episode: Episode) -> str:
        """A time Discord shows in each reader's own zone; just the date when only that is known."""
        if episode.approximate:
            return discord.utils.format_dt(episode.airs_at, "D")
        stamp = episode.airs_at
        return f"{discord.utils.format_dt(stamp, 'R')} ({discord.utils.format_dt(stamp, 'f')})"

    def _line(self, item: Followed, locale: str) -> str:
        """One ``/watching`` row."""
        sub = item.subscription
        episode = episode_from_payload(item.payload)
        where = f"<#{sub.channel_id}>" if sub.channel_id else t("shows.where_dm", locale)
        lead = (
            f" · {t('shows.lead', locale, lead=format_lead(sub.lead_seconds))}"
            if sub.lead_seconds
            else ""
        )
        nxt = (
            f"{self._label(episode, locale)} {self._when(episode)}"
            if episode
            else t("shows.waiting", locale)
        )
        return f"`{sub.id}` **{clip(plain(sub.show_name), _ROW_NAME)}** · {nxt} · {where}{lead}"

    # -- delivery ----------------------------------------------------------------------------

    async def deliver(self, job: Job) -> None:
        """Run a subscription's job: announce the episode, then schedule the next one."""
        store = self._store
        sub_id = job.payload.get("sub")
        if store is None or not isinstance(sub_id, int):
            return
        sub = await store.get(sub_id)
        if sub is None or sub.job_id != job.id:
            return  # cancelled, or this job was already superseded
        episode = episode_from_payload(job.payload)
        if episode is None:
            await self._plan_next(store, sub, job.id, after=None)
            return
        try:
            current = await self._client.episode(episode.id)
        except TvmazeError as error:
            log.warning("Episode check failed, trusting the stored time: %s", error)
            current = episode
        if current is None:
            await self._plan_next(store, sub, job.id, after=episode.id)
            return
        fire_at = current.airs_at - timedelta(seconds=sub.lead_seconds)
        if fire_at > utcnow() + SLACK:  # postponed: wait for the new time instead
            await store.advance(sub.id, job.id, fire_at, episode_payload(current))
            return
        fresh = await store.get(sub.id)
        if fresh is None or fresh.job_id != job.id:
            return  # cancelled or purged while TVmaze was being asked
        try:
            await self._announce(sub, current, job.id)
        except discord.HTTPException as error:
            if job.attempts < REQUEUE_AFTER_ATTEMPTS:
                raise  # let the scheduler retry with its back-off
            log.warning(
                "Show reminder %s: Discord answered %s on attempt %d; trying again later",
                job.id,
                error.status,
                job.attempts,
            )
            await store.advance(sub.id, job.id, utcnow() + RECHECK_SOON, episode_payload(current))
            return
        await self._plan_next(store, sub, job.id, after=current.id)

    async def _announce(self, sub: Subscription, episode: Episode, job_id: int) -> None:
        text = t(
            "shows.notify",
            sub.locale,
            user=f"<@{sub.user_id}>",
            show=plain(sub.show_name),
            episode=self._label(episode, sub.locale),
            when=self._when(episode),
        )
        await send_to_user(
            self.bot,
            user_id=sub.user_id,
            text=text,
            channel_id=sub.channel_id,
            nonce=job_id,
            label=f"Show reminder {job_id}",
        )

    async def _plan_next(
        self, store: ShowStore, sub: Subscription, job_id: int, *, after: int | None
    ) -> None:
        """Schedule the show's next episode, a re-check, or end the subscription."""
        try:
            schedule = await self._client.schedule(sub.show_id, not_next=after)
        except TvmazeError as error:
            log.warning("Next-episode lookup failed, retrying later: %s", error)
            await store.advance(sub.id, job_id, utcnow() + RECHECK_SOON, {})
            return
        if schedule is None:
            await store.drop(sub.id, job_id)
            log.info(
                "Show %s no longer exists at TVmaze; subscription %s ended", sub.show_id, sub.id
            )
            return
        upcoming = schedule.next_episode
        if upcoming is not None and upcoming.id != after:
            lead = timedelta(seconds=sub.lead_seconds)
            run_at = max(utcnow(), upcoming.airs_at - lead)
            await store.advance(sub.id, job_id, run_at, episode_payload(upcoming))
        elif upcoming is None and schedule.show.ended:
            # Tell them first: if the notice fails the retry still finds the subscription.
            await self._announce_end(sub)
            await store.drop(sub.id, job_id)
        else:
            wait = RECHECK_SOON if upcoming is not None else RECHECK_EMPTY
            await store.advance(sub.id, job_id, utcnow() + wait, {})

    async def _announce_end(self, sub: Subscription) -> None:
        text = t("shows.finished", sub.locale, user=f"<@{sub.user_id}>", show=plain(sub.show_name))
        await send_to_user(
            self.bot,
            user_id=sub.user_id,
            text=text,
            channel_id=sub.channel_id,
            nonce=f"end{sub.id}",  # not a job id: a plain number could collide with one
            label=f"Show {sub.id} ended notice",
        )


async def setup(bot: AhaBot) -> None:
    """Add the cog and register its job handler when a scheduler exists."""
    cog = Shows(bot)
    await bot.add_cog(cog)
    if bot.scheduler is not None:
        bot.scheduler.register(KIND, cog.deliver)
