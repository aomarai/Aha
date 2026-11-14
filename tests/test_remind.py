"""Offline tests for the reminders cog, against a fake job store."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import discord
import pytest

from aha.cogs import remind as remind_module
from aha.cogs.remind import KIND, MAX_ACTIVE, MAX_HORIZON, TEXT_LIMIT, Reminders
from aha.cogs.remind import setup as remind_setup
from aha.jobstore import Job
from tests.helpers import Named, StubContext, as_context, http_error

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


class FakeStore:
    """Records what the cog asks of the job store."""

    def __init__(self, jobs: list[Job] | None = None, cancels: bool = True) -> None:
        self.jobs = jobs or []
        self.cancels = cancels
        self.enqueued: list[tuple[str, datetime, dict[str, Any]]] = []
        self.calls: list[tuple[Any, ...]] = []

    async def enqueue(self, kind: str, run_at: datetime, **kwargs: Any) -> int:
        self.enqueued.append((kind, run_at, kwargs))
        return 7

    async def count(self, kind: str, user_id: int) -> int:
        self.calls.append(("count", kind, user_id))
        return len(self.jobs)

    async def list_for_user(self, kind: str, user_id: int, limit: int) -> list[Job]:
        self.calls.append(("list", kind, user_id, limit))
        return self.jobs

    async def cancel(self, kind: str, user_id: int, job_id: int) -> bool:
        self.calls.append(("cancel", kind, user_id, job_id))
        return self.cancels


class FakeChannel(discord.abc.Messageable):
    """A channel that records sends, optionally failing them."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def _get_channel(self) -> Any:  # pragma: no cover - never reached
        raise NotImplementedError

    async def send(self, content: str | None = None, **kwargs: Any) -> Any:
        if self.error is not None:
            raise self.error
        self.sent.append((cast("str", content), kwargs))


class FakeBot:
    """Exposes what the cog uses of ``AhaBot``."""

    def __init__(
        self, channel: Any = None, user: FakeChannel | None = None, db: Any = None
    ) -> None:
        self.db = db
        self.scheduler: Any = None
        self.channel = channel
        self.user = user or FakeChannel()
        self.asked_channels: list[Any] = []
        self.asked_users: list[int] = []
        self.cogs: list[Any] = []

    def get_channel(self, channel_id: int) -> Any:
        self.asked_channels.append(channel_id)
        return self.channel

    async def fetch_user(self, user_id: int) -> Any:
        self.asked_users.append(user_id)
        return self.user

    async def add_cog(self, cog: Any) -> None:
        self.cogs.append(cog)


def make(store: FakeStore | None = None, bot: FakeBot | None = None) -> Reminders:
    return Reminders(cast("Any", bot or FakeBot()), cast("Any", store))


def call(cog: Reminders, command: Any, ctx: StubContext, *args: Any, **kwargs: Any) -> None:
    asyncio.run(cast("Any", command.callback)(cog, as_context(ctx), *args, **kwargs))


def job(job_id: int = 1, text: str = "stretch", user_id: int = 33, **payload: Any) -> Job:
    return Job(
        id=job_id,
        kind=KIND,
        run_at=NOW + timedelta(hours=1),
        user_id=user_id,
        guild_id=11,
        payload={"channel_id": 22, "text": text, **payload},
        attempts=1,
    )


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remind_module, "utcnow", lambda: NOW)


def error_text(ctx: StubContext) -> str:
    ((_, kwargs),) = ctx.replies
    assert kwargs["ephemeral"] is True
    return cast("str", kwargs["embed"].description)


# ----------------------------------------------------------------------------- /remind


def test_remind_stores_a_reminder_and_confirms_privately() -> None:
    store = FakeStore()
    ctx = StubContext()

    call(make(store), Reminders.remind, ctx, "2h", what="  call mum  ")

    ((kind, run_at, kwargs),) = store.enqueued
    assert kind == "reminder"
    assert run_at == NOW + timedelta(hours=2)
    assert kwargs == {
        "user_id": 33,
        "guild_id": 11,
        "payload": {"channel_id": 22, "text": "call mum"},
    }
    assert ("count", "reminder", 33) in store.calls
    ((_, reply_kwargs),) = ctx.replies
    assert reply_kwargs["ephemeral"] is True
    assert reply_kwargs["embed"].description == (
        f"I'll remind you {discord.utils.format_dt(run_at, 'R')} (reminder `7`)."
    )


def test_a_dm_reminder_has_no_guild() -> None:
    store = FakeStore()

    call(make(store), Reminders.remind, StubContext(guild=False), "5m", what="x")

    assert store.enqueued[0][2]["guild_id"] is None


def test_remind_without_a_database_says_so_and_stores_nothing() -> None:
    ctx = StubContext()

    call(make(None), Reminders.remind, ctx, "5m", what="x")

    assert error_text(ctx) == "Reminders are not available: no database is configured."


def test_a_bad_duration_is_explained() -> None:
    store = FakeStore()
    ctx = StubContext()

    call(make(store), Reminders.remind, ctx, "soon", what="x")

    assert error_text(ctx) == "Use a duration like `30m`, `2h` or `1d 12h`."
    assert store.enqueued == []


def test_the_horizon_is_enforced_inclusively() -> None:
    store = FakeStore()
    ctx = StubContext()

    call(make(store), Reminders.remind, ctx, f"{MAX_HORIZON.days + 1}d", what="x")
    assert error_text(ctx) == "Reminders can be at most 365 days ahead."
    assert store.enqueued == []

    call(make(store), Reminders.remind, StubContext(), f"{MAX_HORIZON.days}d", what="x")
    assert len(store.enqueued) == 1


@pytest.mark.parametrize("text", ["", "   ", "x" * (TEXT_LIMIT + 1)])
def test_empty_or_oversized_text_is_rejected(text: str) -> None:
    store = FakeStore()
    ctx = StubContext()

    call(make(store), Reminders.remind, ctx, "5m", what=text)

    assert error_text(ctx) == "Say what to remind you of (up to 1000 characters)."
    assert store.enqueued == []


def test_text_at_the_limit_is_accepted() -> None:
    store = FakeStore()

    call(make(store), Reminders.remind, StubContext(), "5m", what="x" * TEXT_LIMIT)

    assert len(store.enqueued) == 1


def test_the_per_user_cap_is_enforced() -> None:
    full = FakeStore(jobs=[job(i) for i in range(MAX_ACTIVE)])
    ctx = StubContext()

    call(make(full), Reminders.remind, ctx, "5m", what="x")

    assert error_text(ctx) == "You already have 25 reminders. Cancel one with `/unremind`."
    assert full.enqueued == []

    room = FakeStore(jobs=[job(i) for i in range(MAX_ACTIVE - 1)])
    call(make(room), Reminders.remind, StubContext(), "5m", what="x")
    assert len(room.enqueued) == 1


# -------------------------------------------------------------------------- /reminders


def test_reminders_lists_the_callers_own_soonest_first_as_plain_text() -> None:
    store = FakeStore(jobs=[job(3, "take **out** the bin"), job(9, "a\n  b" + "z" * 100)])
    ctx = StubContext()

    call(make(store), Reminders.reminders, ctx)

    assert ("list", "reminder", 33, MAX_ACTIVE) in store.calls
    lines = error_text(ctx).split("\n")
    when = discord.utils.format_dt(NOW + timedelta(hours=1), "R")
    assert lines[0] == f"`3` {when}: take \\*\\*out\\*\\* the bin"
    assert lines[1].startswith(f"`9` {when}: a b" + "z" * 56)
    assert lines[1].endswith("…")
    assert len(lines[1].split(": ", 1)[1]) == 60


def test_a_preview_exactly_at_the_limit_is_not_truncated() -> None:
    store = FakeStore(jobs=[job(1, "y" * 60)])
    ctx = StubContext()

    call(make(store), Reminders.reminders, ctx)

    assert error_text(ctx).endswith(": " + "y" * 60)


def test_reminders_with_none_says_so() -> None:
    ctx = StubContext()

    call(make(FakeStore()), Reminders.reminders, ctx)

    assert error_text(ctx) == "You have no reminders."


def test_reminders_without_a_database_says_so() -> None:
    ctx = StubContext()

    call(make(None), Reminders.reminders, ctx)

    assert error_text(ctx) == "Reminders are not available: no database is configured."


# --------------------------------------------------------------------------- /unremind


def test_unremind_cancels_only_as_the_caller() -> None:
    store = FakeStore()
    ctx = StubContext()

    call(make(store), Reminders.unremind, ctx, 5)

    assert ("cancel", "reminder", 33, 5) in store.calls
    assert error_text(ctx) == "Reminder cancelled."


def test_unremind_of_someone_elses_or_a_missing_reminder_is_refused() -> None:
    ctx = StubContext()

    call(make(FakeStore(cancels=False)), Reminders.unremind, ctx, 5)

    assert error_text(ctx) == "You have no reminder with that id."


def test_unremind_without_a_database_says_so() -> None:
    ctx = StubContext()

    call(make(None), Reminders.unremind, ctx, 5)

    assert error_text(ctx) == "Reminders are not available: no database is configured."


def test_autocomplete_offers_the_callers_matching_reminders() -> None:
    store = FakeStore(jobs=[job(1, "Buy milk"), job(2, "call mum"), job(3, "milk the cow")])
    cog = make(store)
    interaction = cast("Any", Named(user=Named(id=33)))

    choices = asyncio.run(cast("Any", cog).reminder_autocomplete(interaction, " MILK "))

    assert [(c.name, c.value) for c in choices] == [(("1: Buy milk"), 1), ("3: milk the cow", 3)]
    assert ("list", "reminder", 33, MAX_ACTIVE) in store.calls


def test_autocomplete_is_capped_at_discords_limit() -> None:
    store = FakeStore(jobs=[job(i, "x") for i in range(30)])
    interaction = cast("Any", Named(user=Named(id=33)))

    assert len(asyncio.run(cast("Any", make(store)).reminder_autocomplete(interaction, ""))) == 25


def test_autocomplete_without_a_database_offers_nothing() -> None:
    interaction = cast("Any", Named(user=Named(id=33)))

    assert asyncio.run(cast("Any", make(None)).reminder_autocomplete(interaction, "")) == []


# ---------------------------------------------------------------------------- delivery


def test_delivery_posts_in_the_channel_pinging_only_the_invoker() -> None:
    channel = FakeChannel()
    bot = FakeBot(channel=channel)

    asyncio.run(make(FakeStore(), bot).deliver(job(12, "stand *up*")))

    assert bot.asked_channels == [22]
    ((text, kwargs),) = channel.sent
    assert text == "<@33> ⏰ stand \\*up\\*"
    assert kwargs["nonce"] == 12
    mentions = kwargs["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.roles is False
    assert [user.id for user in mentions.users] == [33]
    assert bot.asked_users == []


def test_a_forbidden_or_missing_channel_falls_back_to_a_dm(
    caplog: pytest.LogCaptureFixture,
) -> None:
    for error in (
        discord.Forbidden(cast("Any", Named(status=403, reason="no")), "no"),
        discord.NotFound(cast("Any", Named(status=404, reason="gone")), "gone"),
    ):
        bot = FakeBot(channel=FakeChannel(error))
        with caplog.at_level(logging.INFO, logger="aha.cogs.remind"):
            asyncio.run(make(FakeStore(), bot).deliver(job(12)))
        assert bot.asked_users == [33]
        ((text, kwargs),) = bot.user.sent
        assert text == "<@33> ⏰ stretch"
        assert kwargs["nonce"] == 12
    assert "Reminder 12: channel unavailable, falling back to a DM" in caplog.messages


def test_a_gone_channel_goes_straight_to_a_dm() -> None:
    bot = FakeBot(channel=None)

    asyncio.run(make(FakeStore(), bot).deliver(job()))

    assert bot.asked_users == [33]
    assert len(bot.user.sent) == 1


def test_a_non_integer_channel_id_is_not_looked_up() -> None:
    bot = FakeBot(channel=FakeChannel())

    asyncio.run(make(FakeStore(), bot).deliver(job(channel_id="22")))

    assert bot.asked_channels == []
    assert bot.asked_users == [33]


def test_a_channel_that_cannot_take_messages_falls_back_to_a_dm() -> None:
    bot = FakeBot(channel=Named(id=22))

    asyncio.run(make(FakeStore(), bot).deliver(job()))

    assert bot.asked_users == [33]


def test_closed_dms_drop_the_reminder_and_log_it(caplog: pytest.LogCaptureFixture) -> None:
    closed = discord.Forbidden(cast("Any", Named(status=403, reason="no")), "no")
    bot = FakeBot(channel=None, user=FakeChannel(closed))

    with caplog.at_level(logging.WARNING, logger="aha.cogs.remind"):
        asyncio.run(make(FakeStore(), bot).deliver(job(12)))

    assert caplog.messages == ["Reminder 12 dropped: the channel and the DM are both unreachable"]


def test_other_discord_errors_propagate_so_the_scheduler_retries() -> None:
    bot = FakeBot(channel=FakeChannel(http_error()))

    with pytest.raises(discord.HTTPException):
        asyncio.run(make(FakeStore(), bot).deliver(job()))
    assert bot.asked_users == []


def test_a_dm_failure_other_than_closed_dms_propagates() -> None:
    bot = FakeBot(channel=None, user=FakeChannel(http_error()))

    with pytest.raises(discord.HTTPException):
        asyncio.run(make(FakeStore(), bot).deliver(job()))


# ------------------------------------------------------------------------------ setup


def test_setup_adds_the_cog_and_registers_the_handler() -> None:
    bot = FakeBot()
    registered: list[tuple[str, Any]] = []
    bot.scheduler = Named(register=lambda kind, handler: registered.append((kind, handler)))

    asyncio.run(remind_setup(cast("Any", bot)))

    (cog,) = bot.cogs
    assert isinstance(cog, Reminders)
    assert registered == [("reminder", cog.deliver)]


def test_setup_without_a_scheduler_only_adds_the_cog() -> None:
    bot = FakeBot()

    asyncio.run(remind_setup(cast("Any", bot)))

    assert len(bot.cogs) == 1


def test_the_cog_builds_its_own_store_when_the_bot_has_a_database() -> None:
    cog = Reminders(cast("Any", FakeBot(db=Named())))

    assert cog._store is not None
    assert Reminders(cast("Any", FakeBot()))._store is None


def test_a_job_without_a_user_is_ignored() -> None:
    channel = FakeChannel()
    bot = FakeBot(channel=channel)
    orphan = Job(1, KIND, NOW, None, 11, {"text": "x"}, 1)

    asyncio.run(make(FakeStore(), bot).deliver(orphan))

    assert channel.sent == []
    assert bot.asked_users == []


def test_utcnow_is_timezone_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()

    now = remind_module.utcnow()

    assert now.tzinfo is UTC
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5


def test_a_reminder_missing_its_text_still_delivers_an_empty_one() -> None:
    channel = FakeChannel()
    bot = FakeBot(channel=channel)
    textless = Job(1, KIND, NOW, 33, 11, {"channel_id": 22}, 1)

    asyncio.run(make(FakeStore(), bot).deliver(textless))

    assert [text for text, _ in channel.sent] == ["<@33> ⏰ "]


def test_the_store_is_built_over_the_bots_database() -> None:
    database = Named()

    cog = Reminders(cast("Any", FakeBot(db=database)))

    assert cog._store is not None
    assert cast("Any", cog._store)._db is database


# ------------------------------------------------------------------------- German


def german(ctx: StubContext) -> StubContext:
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)
    return ctx


def test_reminder_replies_follow_the_users_language() -> None:
    store = FakeStore()
    ctx = german(StubContext())
    call(make(store), Reminders.remind, ctx, "2h", what="x")
    assert error_text(ctx) == (
        f"Ich erinnere dich {discord.utils.format_dt(NOW + timedelta(hours=2), 'R')} "
        "(Erinnerung `7`)."
    )

    for args, expected in (
        (("soon",), "Nutze eine Dauer wie `30m`, `2h` oder `1d 12h`."),
        (("0m",), "Die Dauer muss länger als null sein."),
        (("366d",), "Erinnerungen können höchstens 365 Tage im Voraus gesetzt werden."),
    ):
        ctx = german(StubContext())
        call(make(FakeStore()), Reminders.remind, ctx, *args, what="x")
        assert error_text(ctx) == expected

    ctx = german(StubContext())
    call(make(FakeStore()), Reminders.remind, ctx, "5m", what="")
    assert error_text(ctx) == "Sag, woran ich dich erinnern soll (höchstens 1000 Zeichen)."

    ctx = german(StubContext())
    call(
        make(FakeStore(jobs=[job(i) for i in range(MAX_ACTIVE)])),
        Reminders.remind,
        ctx,
        "5m",
        what="x",
    )
    assert error_text(ctx) == "Du hast bereits 25 Erinnerungen. Lösche eine mit `/unremind`."


def test_the_other_reminder_commands_and_the_missing_database_speak_german() -> None:
    cases = [
        (Reminders.reminders, (), FakeStore(), "Du hast keine Erinnerungen."),
        (Reminders.unremind, (5,), FakeStore(), "Erinnerung gelöscht."),
        (
            Reminders.unremind,
            (5,),
            FakeStore(cancels=False),
            "Du hast keine Erinnerung mit dieser ID.",
        ),
        (
            Reminders.reminders,
            (),
            None,
            "Erinnerungen sind nicht verfügbar: Es ist keine Datenbank konfiguriert.",
        ),
    ]
    for command, args, store, expected in cases:
        ctx = german(StubContext())
        call(make(store), command, ctx, *args)
        assert error_text(ctx) == expected
