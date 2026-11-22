"""Offline tests for the show-reminder cog (/watch), against a fake store and a fake TVmaze."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import discord
import pytest

from aha.cogs import shows as shows_module
from aha.cogs.shows import (
    MAX_ACTIVE,
    MAX_LEAD,
    RECHECK_EMPTY,
    RECHECK_SOON,
    SLACK,
    Shows,
    episode_from_payload,
    episode_payload,
    format_lead,
)
from aha.cogs.shows import setup as shows_setup
from aha.jobstore import Job
from aha.showstore import KIND, Followed, Subscription
from aha.tvmaze import Episode, Schedule, Show, TvmazeClient, TvmazeError
from tests.helpers import Named, StubContext, as_context, http_error

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
SHOW = Show(44933, "Severance", "Running", 2022, "Apple TV")
ENDED = Show(82, "Old Show", "Ended", 2011, None)


def episode(
    episode_id: int = 500,
    airs_in: timedelta = timedelta(days=2),
    *,
    title: str = "Woe's Hollow",
    season: int | None = 2,
    number: int | None = 4,
    approximate: bool = False,
) -> Episode:
    return Episode(episode_id, title, season, number, NOW + airs_in, approximate)


class FakeStore:
    """Records what the cog asks of the show store."""

    def __init__(self) -> None:
        self.added: list[dict[str, Any]] = []
        self.add_result: int | None = 9
        self.total = 0
        self.followed: list[Followed] = []
        self.subs: dict[int, Subscription] = {}
        self.removes = True
        self.advanced: list[tuple[int, int, datetime, dict[str, Any]]] = []
        self.dropped: list[tuple[int, int]] = []
        self.drop_result = True
        self.calls: list[tuple[Any, ...]] = []

    async def add(self, **kwargs: Any) -> int | None:
        self.added.append(kwargs)
        return self.add_result

    async def count(self, user_id: int) -> int:
        self.calls.append(("count", user_id))
        return self.total

    async def list_for_user(self, user_id: int, limit: int) -> list[Followed]:
        self.calls.append(("list", user_id, limit))
        return self.followed

    async def remove(self, user_id: int, sub_id: int) -> bool:
        self.calls.append(("remove", user_id, sub_id))
        return self.removes

    async def get(self, sub_id: int) -> Subscription | None:
        self.calls.append(("get", sub_id))
        return self.subs.get(sub_id)

    async def advance(
        self, sub_id: int, expected: int, run_at: datetime, payload: dict[str, Any]
    ) -> bool:
        self.advanced.append((sub_id, expected, run_at, payload))
        return True

    async def drop(self, sub_id: int, expected: int) -> bool:
        self.dropped.append((sub_id, expected))
        return self.drop_result


class FakeClient:
    """A scripted TVmaze."""

    def __init__(self) -> None:
        self.found: list[Show] = [SHOW]
        self.schedules: dict[int, Schedule | Exception | None] = {}
        self.episodes: dict[int, Episode | Exception | None] = {}
        self.calls: list[tuple[str, Any]] = []
        self.delay = 0.0
        self.search_error: Exception | None = None

    async def search(self, query: str) -> list[Show]:
        self.calls.append(("search", query))
        await asyncio.sleep(self.delay)
        if self.search_error is not None:
            raise self.search_error
        return self.found

    async def schedule(self, show_id: int) -> Schedule | None:
        self.calls.append(("schedule", show_id))
        answer = self.schedules.get(show_id)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def episode(self, episode_id: int) -> Episode | None:
        self.calls.append(("episode", episode_id))
        answer = self.episodes.get(episode_id)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeChannel(discord.abc.Messageable):
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
    def __init__(self, channel: Any = None, db: Any = None) -> None:
        self.db = db
        self.scheduler: Any = None
        self.channel = channel
        self.user = FakeChannel()
        self.cogs: list[Any] = []

    def get_channel(self, channel_id: int) -> Any:
        return self.channel

    async def fetch_user(self, user_id: int) -> Any:
        return self.user

    async def add_cog(self, cog: Any) -> None:
        self.cogs.append(cog)


def make(
    store: FakeStore | None = None,
    client: FakeClient | None = None,
    bot: FakeBot | None = None,
) -> Shows:
    return Shows(cast("Any", bot or FakeBot()), cast("Any", store), cast("Any", client))


def run_watch(cog: Shows, ctx: StubContext, *args: Any, **kwargs: Any) -> None:
    asyncio.run(cast("Any", cog.watch.callback)(cog, as_context(ctx), *args, **kwargs))


def reply_of(ctx: StubContext) -> str:
    ((_, kwargs),) = ctx.replies
    assert kwargs["ephemeral"] is True
    return cast("str", kwargs["embed"].description)


def sub(
    sub_id: int = 9,
    *,
    job_id: int | None = 70,
    channel_id: int | None = None,
    lead_seconds: int = 0,
    locale: str = "en-US",
) -> Subscription:
    return Subscription(
        id=sub_id,
        user_id=33,
        guild_id=11 if channel_id else None,
        channel_id=channel_id,
        show_id=SHOW.id,
        show_name="Severance",
        lead_seconds=lead_seconds,
        locale=locale,
        job_id=job_id,
    )


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shows_module, "utcnow", lambda: NOW)


# ----------------------------------------------------------------------------- helpers


def test_the_clock_seam_is_timezone_aware_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the autouse fixture froze it

    before = datetime.now(UTC)
    reading = shows_module.utcnow()

    assert reading.utcoffset() == timedelta(0)
    assert before <= reading <= datetime.now(UTC)


def test_an_episode_survives_the_trip_through_a_job_payload() -> None:
    original = episode(approximate=True, season=None, number=None)

    assert episode_from_payload(episode_payload(original)) == original


def test_the_payload_is_plain_json_data() -> None:
    assert episode_payload(episode()) == {
        "ep": {
            "id": 500,
            "title": "Woe's Hollow",
            "season": 2,
            "number": 4,
            "at": "2030-01-03T12:00:00+00:00",
            "approx": False,
        }
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"ep": None},
        {"ep": "x"},
        {
            "ep": {
                "id": "x",
                "title": "",
                "season": None,
                "number": None,
                "at": "2030-01-01T00:00:00+00:00",
                "approx": False,
            }
        },
        {
            "ep": {
                "id": 1,
                "title": "",
                "season": None,
                "number": None,
                "at": "bad",
                "approx": False,
            }
        },
        {"ep": {"id": 1}},
    ],
)
def test_an_unreadable_payload_has_no_episode(payload: dict[str, Any]) -> None:
    assert episode_from_payload(payload) is None


def test_lead_times_are_written_in_the_largest_whole_unit() -> None:
    assert [format_lead(s) for s in (172800, 7200, 3600, 2700, 60, 90, 45)] == [
        "2d",
        "2h",
        "1h",
        "45m",
        "1m",
        "90s",
        "45s",
    ]


# ----------------------------------------------------------------------------- /watch


def test_watch_without_a_database_says_so_and_does_not_defer() -> None:
    ctx = StubContext()

    run_watch(make(None), ctx, "Severance")

    assert reply_of(ctx) == "Show reminders are not available: no database is configured."
    assert ctx.deferred == []


def test_watch_follows_the_top_match_by_direct_message() -> None:
    store, client = FakeStore(), FakeClient()
    client.schedules[SHOW.id] = Schedule(SHOW, episode())
    ctx = StubContext()

    run_watch(make(store, client), ctx, "  Severance  ")

    assert ctx.deferred == [True]  # the lookups may take longer than Discord waits
    assert client.calls == [("search", "Severance"), ("schedule", SHOW.id)]
    (added,) = store.added
    assert added == {
        "user_id": 33,
        "guild_id": None,
        "channel_id": None,
        "show_id": SHOW.id,
        "show_name": "Severance",
        "lead_seconds": 0,
        "locale": "en-US",
        "run_at": NOW + timedelta(days=2),
        "payload": episode_payload(episode()),
    }
    text = reply_of(ctx)
    assert text.startswith("You'll get reminders for **Severance** (Apple TV, 2022).")
    assert "Next: S2E04 “Woe's Hollow”" in text
    assert text.endswith("Schedules from TVmaze.")


def test_a_show_picked_from_autocomplete_is_looked_up_by_id_without_a_search() -> None:
    store, client = FakeStore(), FakeClient()
    client.schedules[SHOW.id] = Schedule(SHOW, episode())

    run_watch(make(store, client), StubContext(), str(SHOW.id))

    assert client.calls == [("schedule", SHOW.id), ("schedule", SHOW.id)]
    assert store.added[0]["show_id"] == SHOW.id


def test_watch_can_ping_in_the_channel_and_remembers_the_server() -> None:
    store, client = FakeStore(), FakeClient()
    client.schedules[SHOW.id] = Schedule(SHOW, episode())

    run_watch(make(store, client), StubContext(), "Severance", "channel")

    assert (store.added[0]["guild_id"], store.added[0]["channel_id"]) == (11, 22)


def test_a_lead_time_moves_the_reminder_earlier_and_is_stored() -> None:
    store, client = FakeStore(), FakeClient()
    client.schedules[SHOW.id] = Schedule(SHOW, episode(airs_in=timedelta(days=2)))

    run_watch(make(store, client), StubContext(), "Severance", "dm", "3h")

    assert store.added[0]["run_at"] == NOW + timedelta(days=2) - timedelta(hours=3)
    assert store.added[0]["lead_seconds"] == 3 * 3600


def test_a_lead_that_already_passed_reminds_at_once() -> None:
    store, client = FakeStore(), FakeClient()
    client.schedules[SHOW.id] = Schedule(SHOW, episode(airs_in=timedelta(minutes=30)))

    run_watch(make(store, client), StubContext(), "Severance", "dm", "1h")

    assert store.added[0]["run_at"] == NOW


def test_a_bad_lead_is_explained_and_nothing_is_stored() -> None:
    store, client = FakeStore(), FakeClient()
    ctx = StubContext()

    run_watch(make(store, client), ctx, "Severance", "dm", "soon")

    assert reply_of(ctx) == "Use a duration like `30m`, `2h` or `1d 12h`."
    assert store.added == [] and client.calls == []


def test_a_lead_over_a_week_is_refused() -> None:
    ctx = StubContext()

    run_watch(make(FakeStore(), FakeClient()), ctx, "Severance", "dm", "8d")

    assert reply_of(ctx) == "The reminder can come at most 7 days before the episode."


def test_a_lead_of_exactly_a_week_is_fine() -> None:
    store, client = FakeStore(), FakeClient()
    client.schedules[SHOW.id] = Schedule(SHOW, episode(airs_in=timedelta(days=10)))

    run_watch(make(store, client), StubContext(), "Severance", "dm", "7d")

    assert store.added[0]["lead_seconds"] == int(MAX_LEAD.total_seconds())


def test_pinging_in_a_channel_needs_a_server() -> None:
    ctx = StubContext(guild=False)

    run_watch(make(FakeStore(), FakeClient()), ctx, "Severance", "channel")

    assert (
        reply_of(ctx)
        == "Pinging you in a channel only works in a server. Pick direct message instead."
    )


def test_a_direct_message_works_outside_a_server() -> None:
    store, client = FakeStore(), FakeClient()
    client.schedules[SHOW.id] = Schedule(SHOW, episode())

    run_watch(make(store, client), StubContext(guild=False), "Severance")

    assert (store.added[0]["guild_id"], store.added[0]["channel_id"]) == (None, None)


def test_the_limit_is_enforced_before_any_lookup() -> None:
    store, client = FakeStore(), FakeClient()
    store.total = MAX_ACTIVE
    ctx = StubContext()

    run_watch(make(store, client), ctx, "Severance")

    assert reply_of(ctx) == "You already follow 25 shows. Stop one with `/unwatch`."
    assert client.calls == [] and store.added == []


def test_one_below_the_limit_is_allowed() -> None:
    store, client = FakeStore(), FakeClient()
    store.total = MAX_ACTIVE - 1
    client.schedules[SHOW.id] = Schedule(SHOW, episode())

    run_watch(make(store, client), StubContext(), "Severance")

    assert len(store.added) == 1


@pytest.mark.parametrize("found", [[], [SHOW]])
def test_an_unknown_show_is_not_found(found: list[Show]) -> None:
    store, client = FakeStore(), FakeClient()
    client.found = found  # known to search, but TVmaze has no schedule for it
    ctx = StubContext()

    run_watch(make(store, client), ctx, "Severance")

    assert (
        reply_of(ctx)
        == "I couldn't find that show. Start typing its name and pick it from the list."
    )
    assert store.added == []


def test_an_unknown_id_is_not_found() -> None:
    ctx = StubContext()

    run_watch(make(FakeStore(), FakeClient()), ctx, "123")

    assert "couldn't find that show" in reply_of(ctx)


def test_a_failed_lookup_is_reported_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient()
    client.search_error = TvmazeError("the request timed out")
    ctx = StubContext()

    with caplog.at_level(logging.WARNING, logger="aha.cogs.shows"):
        run_watch(make(FakeStore(), client), ctx, "Severance")

    assert reply_of(ctx) == "I couldn't reach the TV schedule right now. Try again in a minute."
    assert caplog.messages == ["Show lookup failed: the request timed out"]


def test_a_failed_schedule_lookup_is_reported_too() -> None:
    client = FakeClient()
    client.schedules[SHOW.id] = TvmazeError("x")
    ctx = StubContext()

    run_watch(make(FakeStore(), client), ctx, "Severance")

    assert "couldn't reach the TV schedule" in reply_of(ctx)


def test_an_ended_show_with_nothing_to_come_is_refused() -> None:
    store, client = FakeStore(), FakeClient()
    client.found = [ENDED]
    client.schedules[ENDED.id] = Schedule(ENDED, None)
    ctx = StubContext()

    run_watch(make(store, client), ctx, "Old Show")

    assert (
        reply_of(ctx) == "**Old Show** has ended, so there are no more episodes to remind you of."
    )
    assert store.added == []


def test_an_ended_show_with_a_final_episode_still_to_air_can_be_followed() -> None:
    store, client = FakeStore(), FakeClient()
    client.found = [ENDED]
    client.schedules[ENDED.id] = Schedule(ENDED, episode())

    run_watch(make(store, client), StubContext(), "Old Show")

    assert len(store.added) == 1


def test_a_show_with_no_date_yet_is_checked_again_tomorrow() -> None:
    store, client = FakeStore(), FakeClient()
    client.schedules[SHOW.id] = Schedule(SHOW, None)
    ctx = StubContext()

    run_watch(make(store, client), ctx, "Severance")

    assert store.added[0]["run_at"] == NOW + RECHECK_EMPTY
    assert store.added[0]["payload"] == {}
    text = reply_of(ctx)
    assert "No new episode has a date yet." in text
    assert "Next:" not in text


def test_following_twice_is_refused() -> None:
    store, client = FakeStore(), FakeClient()
    store.add_result = None
    client.schedules[SHOW.id] = Schedule(SHOW, episode())
    ctx = StubContext()

    run_watch(make(store, client), ctx, "Severance")

    assert reply_of(ctx) == "You already follow **Severance**. See your shows with `/watching`."


def test_a_hostile_show_name_cannot_ping_or_format() -> None:
    store, client = FakeStore(), FakeClient()
    nasty = Show(1, "@everyone **bold**", "Running", None, "`net`")
    client.found = [nasty]
    client.schedules[1] = Schedule(nasty, None)
    ctx = StubContext()

    run_watch(make(store, client), ctx, "x")

    assert reply_of(ctx).startswith(
        "You'll get reminders for **@\u200beveryone \\*\\*bold\\*\\*** ('net')."
    )


def test_the_command_metadata() -> None:
    assert Shows.watch.qualified_name == "watch"
    assert Shows.watching.qualified_name == "watching"
    assert Shows.unwatch.qualified_name == "unwatch"
    for command in (Shows.watch, Shows.watching, Shows.unwatch):
        assert command.extras["category"] == "Info"
    assert [
        (c.cooldown.rate, c.cooldown.per)
        for c in (Shows.watch, Shows.watching, Shows.unwatch)
        if c.cooldown
    ] == [
        (1, 10),
        (1, 5),
        (1, 5),
    ]
    app_command = cast("Any", Shows.watch.app_command)
    where = next(p for p in app_command.parameters if p.name == "where")
    assert [(c.name, c.value) for c in where.choices] == [
        ("Direct message", "dm"),
        ("Ping me in this channel", "channel"),
    ]
    assert where.required is False


# ----------------------------------------------------------------------------- autocomplete


def complete(cog: Shows, current: str) -> list[Any]:
    return asyncio.run(cast("Any", cog).show_autocomplete(cast("Any", Named()), current))


def test_autocomplete_offers_shows_with_their_year_and_the_id_as_value() -> None:
    client = FakeClient()
    client.found = [SHOW, ENDED, Show(7, "No Year", "Running", None, None)]

    choices = complete(make(FakeStore(), client), "sev")

    assert [(c.name, c.value) for c in choices] == [
        ("Severance (2022)", "44933"),
        ("Old Show (2011)", "82"),
        ("No Year", "7"),
    ]


def test_autocomplete_waits_for_two_characters() -> None:
    client = FakeClient()

    assert complete(make(FakeStore(), client), " s ") == []
    assert client.calls == []
    assert len(complete(make(FakeStore(), client), "se")) == 1


def test_autocomplete_is_capped_at_discords_limit_and_names_are_clipped() -> None:
    client = FakeClient()
    client.found = [Show(n, "x" * 200, "Running", 2020, None) for n in range(30)]

    choices = complete(make(FakeStore(), client), "xx")

    assert len(choices) == 25
    assert len(choices[0].name) == 100


def test_autocomplete_stays_quiet_when_tvmaze_fails() -> None:
    client = FakeClient()
    client.search_error = TvmazeError("x")

    assert complete(make(FakeStore(), client), "sev") == []


def test_autocomplete_gives_up_before_discord_does(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient()
    client.delay = 1.0
    monkeypatch.setattr(shows_module, "AUTOCOMPLETE_TIMEOUT", 0.01)

    assert complete(make(FakeStore(), client), "sev") == []


def test_the_autocomplete_budget_is_inside_discords_three_seconds() -> None:
    assert shows_module.AUTOCOMPLETE_TIMEOUT < 3.0


# ----------------------------------------------------------------------------- /watching


def followed(sub_row: Subscription, run_at: datetime | None, payload: dict[str, Any]) -> Followed:
    return Followed(sub_row, run_at, payload)


def test_watching_lists_each_show_with_its_next_episode_and_where() -> None:
    store = FakeStore()
    store.followed = [
        followed(sub(1), NOW, episode_payload(episode())),
        followed(
            sub(2, channel_id=22, lead_seconds=7200),
            NOW,
            episode_payload(episode(approximate=True)),
        ),
        followed(sub(3), NOW, {}),
    ]
    ctx = StubContext()
    cog = make(store)

    asyncio.run(cast("Any", cog.watching.callback)(cog, as_context(ctx)))

    lines = reply_of(ctx).split("\n")
    assert store.calls == [("list", 33, MAX_ACTIVE)]
    assert lines[0].startswith("`1` **Severance** · S2E04 “Woe's Hollow” <t:")
    assert lines[0].endswith("· DM")
    assert "(<t:" in lines[0]  # an exact time shows both relative and absolute
    assert lines[1].startswith("`2` **Severance** · S2E04 “Woe's Hollow” <t:1893672000:D>")
    assert lines[1].endswith("· <#22> · 2h before")
    assert lines[2] == "`3` **Severance** · waiting for a date · DM"
    assert lines[-1] == "Schedules from TVmaze."


def test_watching_with_no_shows_points_at_watch() -> None:
    ctx = StubContext()
    cog = make(FakeStore())

    asyncio.run(cast("Any", cog.watching.callback)(cog, as_context(ctx)))

    assert reply_of(ctx) == "You don't follow any shows. Start with `/watch`."


def test_watching_without_a_database_says_so() -> None:
    ctx = StubContext()
    cog = make(None)

    asyncio.run(cast("Any", cog.watching.callback)(cog, as_context(ctx)))

    assert "not available" in reply_of(ctx)


# ----------------------------------------------------------------------------- /unwatch


def test_unwatch_removes_only_the_callers_own_show() -> None:
    store = FakeStore()
    ctx = StubContext()
    cog = make(store)

    asyncio.run(cast("Any", cog.unwatch.callback)(cog, as_context(ctx), 9))

    assert store.calls == [("remove", 33, 9)]
    assert reply_of(ctx) == "Stopped following that show."


def test_unwatch_of_someone_elses_or_a_missing_show_is_not_found() -> None:
    store = FakeStore()
    store.removes = False
    ctx = StubContext()
    cog = make(store)

    asyncio.run(cast("Any", cog.unwatch.callback)(cog, as_context(ctx), 9))

    assert reply_of(ctx) == "You don't follow a show with that id."


def test_unwatch_without_a_database_says_so() -> None:
    ctx = StubContext()
    cog = make(None)

    asyncio.run(cast("Any", cog.unwatch.callback)(cog, as_context(ctx), 9))

    assert "not available" in reply_of(ctx)


def followed_choices(cog: Shows, current: str, user_id: int = 33) -> list[Any]:
    interaction = cast("Any", Named(user=Named(id=user_id)))
    return asyncio.run(cast("Any", cog).followed_autocomplete(interaction, current))


def test_unwatch_autocomplete_offers_the_callers_matching_shows() -> None:
    store = FakeStore()
    store.followed = [
        followed(sub(1), None, {}),
        followed(Subscription(2, 33, None, None, 5, "Other Show", 0, "en-US", None), None, {}),
    ]

    choices = followed_choices(make(store), " SEVER ")

    assert [(c.name, c.value) for c in choices] == [("Severance", 1)]
    assert store.calls == [("list", 33, MAX_ACTIVE)]


def test_unwatch_autocomplete_is_capped_and_needs_a_database() -> None:
    store = FakeStore()
    store.followed = [followed(sub(n), None, {}) for n in range(30)]

    assert len(followed_choices(make(store), "")) == 25
    assert followed_choices(make(None), "") == []


# ----------------------------------------------------------------------------- delivery


def job(job_id: int = 70, payload: dict[str, Any] | None = None) -> Job:
    data = (
        {"sub": 9, **episode_payload(episode(airs_in=timedelta(0)))} if payload is None else payload
    )
    return Job(
        id=job_id, kind=KIND, run_at=NOW, user_id=33, guild_id=None, payload=data, attempts=1
    )


def deliver(cog: Shows, the_job: Job) -> None:
    asyncio.run(cog.deliver(the_job))


def delivery_setup(
    *,
    sub_row: Subscription | None = None,
    bot: FakeBot | None = None,
) -> tuple[Shows, FakeStore, FakeClient, FakeBot]:
    store, client = FakeStore(), FakeClient()
    store.subs[9] = sub_row or sub()
    the_bot = bot or FakeBot()
    client.episodes[500] = episode(airs_in=timedelta(0))
    client.schedules[SHOW.id] = Schedule(SHOW, episode(501, timedelta(days=7)))
    return make(store, client, the_bot), store, client, the_bot


def test_a_due_episode_is_announced_by_dm_and_the_next_one_scheduled() -> None:
    cog, store, _, bot = delivery_setup()

    deliver(cog, job())

    ((text, kwargs),) = bot.user.sent
    assert text == (
        "<@33> 📺 **Severance**: S2E04 “Woe's Hollow” <t:1893499200:R> (<t:1893499200:f>)"
    )
    assert kwargs["nonce"] == 70
    assert kwargs["allowed_mentions"].users == [discord.Object(id=33)]
    assert kwargs["allowed_mentions"].everyone is False
    assert store.advanced == [
        (9, 70, NOW + timedelta(days=7), episode_payload(episode(501, timedelta(days=7))))
    ]
    assert store.dropped == []


def test_a_channel_subscription_is_announced_in_the_channel() -> None:
    channel = FakeChannel()
    cog, _, _, bot = delivery_setup(sub_row=sub(channel_id=22), bot=FakeBot(channel=channel))

    deliver(cog, job())

    assert len(channel.sent) == 1 and bot.user.sent == []


def test_the_next_reminder_keeps_the_lead_time() -> None:
    cog, store, client, _ = delivery_setup(sub_row=sub(lead_seconds=3600))
    # The current one is an hour away, which is exactly its reminder time.
    client.episodes[500] = episode(airs_in=timedelta(hours=1))

    deliver(cog, job())

    assert store.advanced[0][2] == NOW + timedelta(days=7) - timedelta(hours=1)


def test_a_next_episode_whose_reminder_time_has_passed_is_sent_at_once() -> None:
    cog, store, client, _ = delivery_setup(sub_row=sub(lead_seconds=3600))
    client.episodes[500] = episode(airs_in=timedelta(hours=1))
    client.schedules[SHOW.id] = Schedule(SHOW, episode(501, timedelta(minutes=10)))

    deliver(cog, job())

    assert store.advanced[0][2] == NOW


def test_a_cancelled_subscription_is_ignored() -> None:
    cog, store, client, bot = delivery_setup()
    store.subs.clear()

    deliver(cog, job())

    assert bot.user.sent == [] and store.advanced == [] and client.calls == []


def test_a_superseded_job_is_ignored() -> None:
    cog, store, _, bot = delivery_setup(sub_row=sub(job_id=71))

    deliver(cog, job(70))

    assert bot.user.sent == [] and store.advanced == []


def test_a_job_without_a_subscription_id_or_a_store_is_ignored() -> None:
    cog, store, _, bot = delivery_setup()

    deliver(cog, job(payload={}))
    deliver(cog, job(payload={"sub": "9"}))
    deliver(make(None, FakeClient(), bot), job())

    assert store.calls == [] and bot.user.sent == []


def test_a_postponed_episode_is_rescheduled_not_announced() -> None:
    cog, store, client, bot = delivery_setup()
    later = episode(airs_in=timedelta(days=7))
    client.episodes[500] = later

    deliver(cog, job())

    assert bot.user.sent == []
    assert store.advanced == [(9, 70, later.airs_at, episode_payload(later))]


def test_a_postponement_is_measured_from_the_lead_time() -> None:
    cog, store, client, _ = delivery_setup(sub_row=sub(lead_seconds=3600))
    later = episode(airs_in=timedelta(days=1))
    client.episodes[500] = later

    deliver(cog, job())

    assert store.advanced[0][2] == later.airs_at - timedelta(hours=1)


def test_a_small_shift_within_the_slack_still_announces() -> None:
    cog, _, client, bot = delivery_setup()
    client.episodes[500] = episode(airs_in=SLACK)  # exactly at the edge: not postponed

    deliver(cog, job())

    assert len(bot.user.sent) == 1


def test_just_past_the_slack_is_a_postponement() -> None:
    cog, store, client, bot = delivery_setup()
    client.episodes[500] = episode(airs_in=SLACK + timedelta(seconds=1))

    deliver(cog, job())

    assert bot.user.sent == []
    assert len(store.advanced) == 1


def test_an_episode_that_moved_earlier_is_announced_with_its_real_time() -> None:
    cog, _, client, bot = delivery_setup()
    client.episodes[500] = episode(airs_in=timedelta(hours=-3))

    deliver(cog, job())

    ((text, _),) = bot.user.sent
    assert "<t:1893488400:R>" in text  # three hours before NOW


def test_when_tvmaze_cannot_confirm_the_stored_episode_is_trusted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cog, store, client, bot = delivery_setup()
    client.episodes[500] = TvmazeError("the request timed out")

    with caplog.at_level(logging.WARNING, logger="aha.cogs.shows"):
        deliver(cog, job())

    assert len(bot.user.sent) == 1
    assert (
        "Episode check failed, trusting the stored time: the request timed out" in caplog.messages
    )
    assert len(store.advanced) == 1


def test_an_episode_that_vanished_is_not_announced_and_the_next_one_is_found() -> None:
    cog, store, client, bot = delivery_setup()
    client.episodes[500] = None

    deliver(cog, job())

    assert bot.user.sent == []
    assert [call for call in client.calls if call[0] == "schedule"] == [("schedule", SHOW.id)]
    assert store.advanced[0][3] == episode_payload(episode(501, timedelta(days=7)))


def test_a_vanished_episode_that_is_still_the_next_one_is_rechecked_soon() -> None:
    cog, store, client, _ = delivery_setup()
    client.episodes[500] = None
    client.schedules[SHOW.id] = Schedule(SHOW, episode(500, timedelta(days=1)))

    deliver(cog, job())

    assert store.advanced == [(9, 70, NOW + RECHECK_SOON, {})]


def test_a_job_with_no_episode_looks_for_the_next_one_without_announcing() -> None:
    cog, store, client, bot = delivery_setup()

    deliver(cog, job(payload={"sub": 9}))

    assert bot.user.sent == []
    assert [call[0] for call in client.calls] == ["schedule"]
    assert store.advanced == [
        (9, 70, NOW + timedelta(days=7), episode_payload(episode(501, timedelta(days=7))))
    ]


def test_the_same_episode_again_after_announcing_is_rechecked_in_an_hour() -> None:
    cog, store, client, _ = delivery_setup()
    client.schedules[SHOW.id] = Schedule(SHOW, episode(500, timedelta(minutes=1)))

    deliver(cog, job())

    assert store.advanced == [(9, 70, NOW + RECHECK_SOON, {})]


def test_no_next_episode_yet_means_checking_again_tomorrow() -> None:
    cog, store, client, _ = delivery_setup()
    client.schedules[SHOW.id] = Schedule(SHOW, None)

    deliver(cog, job())

    assert store.advanced == [(9, 70, NOW + RECHECK_EMPTY, {})]
    assert store.dropped == []


def test_a_failed_next_episode_lookup_is_retried_in_an_hour(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cog, store, client, bot = delivery_setup()
    client.schedules[SHOW.id] = TvmazeError("TVmaze answered HTTP 503")

    with caplog.at_level(logging.WARNING, logger="aha.cogs.shows"):
        deliver(cog, job())

    assert len(bot.user.sent) == 1  # the reminder still goes out
    assert store.advanced == [(9, 70, NOW + RECHECK_SOON, {})]
    assert caplog.messages == [
        "Next-episode lookup failed, retrying later: TVmaze answered HTTP 503"
    ]


def test_a_finished_show_ends_the_subscription_and_says_so() -> None:
    cog, store, client, bot = delivery_setup()
    client.schedules[SHOW.id] = Schedule(Show(SHOW.id, "Severance", "Ended", 2022, None), None)

    deliver(cog, job())

    assert store.dropped == [(9, 70)]
    assert store.advanced == []
    assert [text for text, _ in bot.user.sent][-1] == (
        "<@33> 📺 **Severance** has ended, so I've stopped following it."
    )


def test_an_ended_notice_is_only_sent_by_the_job_that_really_ended_it() -> None:
    cog, store, client, bot = delivery_setup()
    store.drop_result = False
    client.schedules[SHOW.id] = Schedule(Show(SHOW.id, "Severance", "Ended", 2022, None), None)

    deliver(cog, job())

    assert len(bot.user.sent) == 1  # the episode announcement only


def test_a_show_that_disappeared_from_tvmaze_ends_quietly(caplog: pytest.LogCaptureFixture) -> None:
    cog, store, client, bot = delivery_setup()
    client.schedules[SHOW.id] = None

    with caplog.at_level(logging.INFO, logger="aha.cogs.shows"):
        deliver(cog, job())

    assert store.dropped == [(9, 70)]
    assert len(bot.user.sent) == 1
    assert "Show 44933 no longer exists at TVmaze; subscription 9 ended" in caplog.messages


def test_the_ended_notice_follows_the_subscribers_language() -> None:
    cog, _, client, bot = delivery_setup(sub_row=sub(locale="de"))
    client.schedules[SHOW.id] = Schedule(Show(SHOW.id, "Severance", "Ended", 2022, None), None)

    deliver(cog, job())

    assert (
        bot.user.sent[-1][0]
        == "<@33> 📺 **Severance** ist beendet, ich folge der Serie nicht mehr."
    )


def test_a_discord_failure_propagates_so_the_scheduler_retries() -> None:
    bot = FakeBot()
    bot.user = FakeChannel(error=http_error())
    cog, store, _, _ = delivery_setup(bot=bot)

    with pytest.raises(discord.HTTPException):
        deliver(cog, job())

    assert store.advanced == []  # nothing moved on: the retry will announce again


# ----------------------------------------------------------------------------- rendering


def test_an_episode_label_degrades_gracefully() -> None:
    label = Shows._label
    assert label(episode(), "en-US") == "S2E04 “Woe's Hollow”"
    assert label(episode(title=""), "en-US") == "S2E04"
    assert label(episode(season=None, number=None), "en-US") == "“Woe's Hollow”"
    assert label(episode(title="", season=None, number=None), "en-US") == "a new episode"
    assert label(None, "en-US") == "a new episode"
    assert label(None, "de") == "eine neue Folge"


def test_an_exact_time_shows_relative_and_absolute_and_a_date_only_one_just_the_day() -> None:
    assert Shows._when(episode()) == "<t:1893672000:R> (<t:1893672000:f>)"
    assert Shows._when(episode(approximate=True)) == "<t:1893672000:D>"


def test_show_details_list_the_network_and_year_when_known() -> None:
    assert Shows._details(SHOW) == "Apple TV, 2022"
    assert Shows._details(Show(1, "A", "Running", 2020, None)) == "2020"
    assert Shows._details(Show(1, "A", "Running", None, "HBO")) == "HBO"
    assert Shows._details(Show(1, "A", "Running", None, None)) == ""


# ----------------------------------------------------------------------------- setup


def test_setup_adds_the_cog_and_registers_the_job_handler() -> None:
    registered: list[tuple[str, Any]] = []
    bot = FakeBot()
    bot.scheduler = Named(register=lambda kind, handler: registered.append((kind, handler)))

    asyncio.run(shows_setup(cast("Any", bot)))

    (cog,) = bot.cogs
    assert isinstance(cog, Shows)
    assert registered == [("show", cog.deliver)]


def test_setup_without_a_scheduler_only_adds_the_cog() -> None:
    bot = FakeBot()

    asyncio.run(shows_setup(cast("Any", bot)))

    assert len(bot.cogs) == 1


def test_the_cog_builds_its_own_store_from_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def make_store(db: Any) -> str:
        seen.append(db)
        return "store"

    monkeypatch.setattr(shows_module, "ShowStore", make_store)
    database = Named()

    cog = Shows(cast("Any", FakeBot(db=database)))

    assert seen == [database]
    assert cast("Any", cog)._store == "store"
    assert cast("Any", Shows(cast("Any", FakeBot())))._store is None


# ----------------------------------------------------------------------------- language


def german(ctx: StubContext) -> StubContext:
    ctx.interaction = Named(locale=discord.Locale.german, guild_locale=None)
    return ctx


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(cast("Any", Named(status=403, reason="no")), "no")


def test_a_default_client_is_a_real_tvmaze_client() -> None:
    assert isinstance(cast("Any", make(FakeStore()))._client, TvmazeClient)


def test_the_unavailable_notice_follows_the_users_language() -> None:
    ctx = german(StubContext())

    run_watch(make(None), ctx, "Severance")

    assert reply_of(ctx) == (
        "Serien-Erinnerungen sind nicht verfügbar: Es ist keine Datenbank konfiguriert."
    )


def test_watch_answers_in_the_users_language_and_remembers_it() -> None:
    store, client = FakeStore(), FakeClient()
    client.schedules[SHOW.id] = Schedule(SHOW, episode())
    ctx = german(StubContext())

    run_watch(make(store, client), ctx, "Severance")

    assert store.added[0]["locale"] == "de"
    assert reply_of(ctx).startswith("Du bekommst Erinnerungen für **Severance** (Apple TV, 2022).")


def test_the_list_follows_the_users_language() -> None:
    store = FakeStore()
    bare = episode(title="", season=None, number=None)
    store.followed = [
        followed(sub(1, lead_seconds=7200), NOW, episode_payload(bare)),
        followed(sub(2), NOW, {}),
    ]
    ctx = german(StubContext())
    cog = make(store)

    asyncio.run(cast("Any", cog.watching.callback)(cog, as_context(ctx)))

    first, second, _, credit = reply_of(ctx).split("\n")
    assert first.startswith("`1` **Severance** · eine neue Folge <t:")
    assert first.endswith("· Direktnachricht · 2h vorher")
    assert second == "`2` **Severance** · wartet auf ein Datum · Direktnachricht"
    assert credit == "Sendepläne von TVmaze."


def test_an_empty_episode_label_is_localised_too() -> None:
    assert Shows._label(episode(title="", season=None, number=None), "de") == "eine neue Folge"


def test_the_announcement_is_in_the_subscribers_language() -> None:
    cog, _, _, bot = delivery_setup(sub_row=sub(locale="de"))

    deliver(cog, job())

    ((text, _),) = bot.user.sent
    assert text == (
        "<@33> 📺 Neue Folge von **Severance**: S2E04 “Woe's Hollow” "
        "<t:1893499200:R> (<t:1893499200:f>)"
    )


def test_an_untitled_episode_is_announced_in_the_subscribers_language() -> None:
    cog, _, client, bot = delivery_setup(sub_row=sub(locale="de"))
    client.episodes[500] = episode(airs_in=timedelta(0), title="", season=None, number=None)

    deliver(cog, job())

    assert bot.user.sent[0][0].startswith("<@33> 📺 Neue Folge von **Severance**: eine neue Folge ")


def test_the_ended_notice_goes_to_the_channel_with_the_subscription_as_its_nonce() -> None:
    channel = FakeChannel()
    cog, _, client, bot = delivery_setup(sub_row=sub(channel_id=22), bot=FakeBot(channel=channel))
    client.schedules[SHOW.id] = Schedule(Show(SHOW.id, "Severance", "Ended", 2022, None), None)

    deliver(cog, job())

    assert bot.user.sent == []
    announcement, ended = channel.sent
    assert announcement[1]["nonce"] == 70  # the job
    assert ended[0] == "<@33> 📺 **Severance** has ended, so I've stopped following it."
    assert ended[1]["nonce"] == 9  # the subscription


def test_unreachable_people_are_logged_with_what_was_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = FakeBot(channel=FakeChannel(error=forbidden()))
    bot.user = FakeChannel(error=forbidden())
    cog, _, client, _ = delivery_setup(sub_row=sub(channel_id=22), bot=bot)
    client.schedules[SHOW.id] = Schedule(Show(SHOW.id, "Severance", "Ended", 2022, None), None)

    with caplog.at_level(logging.INFO, logger="aha.delivery"):
        deliver(cog, job())

    assert caplog.messages == [
        "Show reminder 70: channel unavailable, falling back to a DM",
        "Show reminder 70 dropped: the channel and the DM are both unreachable",
        "Show 9 ended notice: channel unavailable, falling back to a DM",
        "Show 9 ended notice dropped: the channel and the DM are both unreachable",
    ]
