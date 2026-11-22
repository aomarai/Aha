"""Tests for the TVmaze client behind /watch, with a fake HTTP session (no network)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import aiohttp
import pytest

from aha.net import TTLCache
from aha.tvmaze import (
    BASE_URL,
    MAX_RESULTS,
    USER_AGENT,
    Episode,
    Schedule,
    Show,
    TvmazeClient,
    TvmazeError,
    parse_episode,
    parse_show,
)

SHOW = {
    "id": 44933,
    "name": " Severance ",
    "status": "Running",
    "premiered": "2022-02-18",
    "network": None,
    "webChannel": {"name": "Apple TV"},
}
EPISODE = {
    "id": 3804165,
    "name": "Woe's Hollow",
    "season": 2,
    "number": 4,
    "airdate": "2025-01-31",
    "airstamp": "2025-01-31T02:00:00+00:00",
}


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, answers: list[Any], calls: list[dict[str, Any]], headers: Any) -> None:
        self._answers = answers
        self._calls = calls
        self._headers = headers

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def get(self, url: str, *, params: dict[str, str] | None) -> Any:
        self._calls.append({"url": url, "params": params, "headers": self._headers})
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make(
    *answers: Any, clock: Clock | None = None, **kwargs: Any
) -> tuple[TvmazeClient, list[Any]]:
    calls: list[dict[str, Any]] = []
    queue = list(answers)
    client = TvmazeClient(
        session_factory=cast("Any", lambda headers: FakeSession(queue, calls, headers)),
        clock=clock or Clock(),
        **kwargs,
    )
    return client, calls


def ok(body: Any) -> FakeResponse:
    return FakeResponse(200, body)


# ----------------------------------------------------------------------------- parsing


def test_a_show_is_read_with_its_network_year_and_a_trimmed_name() -> None:
    assert parse_show(SHOW) == Show(44933, "Severance", "Running", 2022, "Apple TV")


def test_a_show_prefers_its_network_over_a_web_channel_and_tolerates_gaps() -> None:
    show = parse_show({"id": 1, "name": "A", "network": {"name": "HBO"}, "webChannel": None})
    assert show == Show(1, "A", "", None, "HBO")


def test_long_names_are_clipped() -> None:
    show = parse_show({"id": 1, "name": "x" * 300})
    assert len(show.name) == 100
    assert show.name.endswith("…")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"id": "x", "name": "A"},
        {"id": 1},
        {"id": 1, "name": "A", "premiered": "soon"},
        [],
    ],
)
def test_a_malformed_show_is_an_error(raw: Any) -> None:
    with pytest.raises(TvmazeError) as error:
        parse_show(raw)
    assert str(error.value) == "a show was not in the expected shape"


def test_a_show_without_a_name_is_an_error_saying_so() -> None:
    with pytest.raises(TvmazeError) as error:
        parse_show({"id": 1, "name": "  "})
    assert str(error.value) == "a show has no name"


def test_a_show_with_no_network_or_web_channel_has_none() -> None:
    assert parse_show({"id": 1, "name": "A"}).network is None
    assert parse_show({"id": 1, "name": "A", "network": {"name": ""}}).network is None


def test_only_ended_shows_are_ended() -> None:
    assert Show(1, "A", "Ended", None, None).ended is True
    assert Show(1, "A", "Running", None, None).ended is False


def test_an_episode_is_read_in_utc_with_a_code() -> None:
    episode = parse_episode(EPISODE)
    assert episode == Episode(
        3804165, "Woe's Hollow", 2, 4, datetime(2025, 1, 31, 2, 0, tzinfo=UTC), False
    )
    assert episode is not None
    assert episode.code == "S2E04"


@pytest.fixture
def new_york_clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run with a local time zone that is not UTC, so a stray local-time conversion shows."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.usefixtures("new_york_clock")
def test_an_offset_air_time_is_converted_to_utc() -> None:
    episode = parse_episode({**EPISODE, "airstamp": "2025-01-30T21:00:00-05:00"})
    assert episode is not None
    assert episode.airs_at == datetime(2025, 1, 31, 2, 0, tzinfo=UTC)
    assert episode.airs_at.utcoffset() == timedelta(0)


@pytest.mark.usefixtures("new_york_clock")
def test_a_naive_air_time_is_taken_as_utc_not_local_time() -> None:
    episode = parse_episode({**EPISODE, "airstamp": "2025-01-31T02:00:00"})
    assert episode is not None
    assert episode.airs_at == datetime(2025, 1, 31, 2, 0, tzinfo=UTC)
    assert episode.airs_at.utcoffset() == timedelta(0)


def test_an_episode_with_only_a_date_is_approximate_midday_utc() -> None:
    episode = parse_episode({**EPISODE, "airstamp": None})
    assert episode is not None
    assert episode.airs_at == datetime(2025, 1, 31, 12, 0, tzinfo=UTC)
    assert episode.approximate is True


def test_an_empty_episode_has_no_date_so_it_is_none() -> None:
    assert parse_episode({}) is None


def test_an_episode_without_any_date_is_none() -> None:
    assert parse_episode({"id": 1, "name": "x", "airstamp": None, "airdate": ""}) is None


def test_a_special_has_no_code_and_a_missing_title_is_empty() -> None:
    episode = parse_episode({"id": 1, "airstamp": "2025-01-31T02:00:00+00:00"})
    assert episode is not None
    assert (episode.season, episode.number, episode.title, episode.code) == (None, None, "", None)
    assert Episode(1, "", 2, None, datetime(2025, 1, 1, tzinfo=UTC)).code is None
    assert Episode(1, "", None, 3, datetime(2025, 1, 1, tzinfo=UTC)).code is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"id": "x", "airstamp": "2025-01-31T02:00:00+00:00"},
        {"id": 1, "airstamp": "nope"},
        {"id": 1, "airstamp": None, "airdate": "nope"},
        {"id": 1, "airstamp": "2025-01-31T02:00:00+00:00", "season": "x"},
    ],
)
def test_a_malformed_episode_is_an_error(raw: Any) -> None:
    with pytest.raises(TvmazeError) as error:
        parse_episode(raw)
    assert str(error.value) == "an episode was not in the expected shape"


# ----------------------------------------------------------------------------- search


def test_a_search_asks_for_the_normalised_query_and_identifies_itself() -> None:
    client, calls = make(ok([{"score": 1, "show": SHOW}]))

    shows = asyncio.run(client.search("  SEVERANCE  now "))

    assert shows == [Show(44933, "Severance", "Running", 2022, "Apple TV")]
    assert calls == [
        {
            "url": f"{BASE_URL}/search/shows",
            "params": {"q": "severance now"},
            "headers": {"User-Agent": USER_AGENT},
        }
    ]


def test_a_blank_search_asks_nothing() -> None:
    client, calls = make()
    assert asyncio.run(client.search("   ")) == []
    assert calls == []


def test_a_search_is_cached_for_five_minutes() -> None:
    clock = Clock()
    client, calls = make(ok([{"show": SHOW}]), ok([]), clock=clock)

    first = asyncio.run(client.search("Severance"))
    clock.now = 299.0
    assert asyncio.run(client.search("severance")) == first
    assert len(calls) == 1
    clock.now = 300.0
    assert asyncio.run(client.search("severance")) == []
    assert len(calls) == 2


def test_one_odd_result_does_not_hide_the_others_and_results_are_capped() -> None:
    good = [{"show": {**SHOW, "id": n}} for n in range(1, 16)]
    client, _ = make(ok([{"score": 1}, {"show": None}, {"show": {"id": 1}}, *good]))

    shows = asyncio.run(client.search("x"))

    assert [show.id for show in shows] == list(range(1, MAX_RESULTS + 1))


def test_a_search_answer_that_is_not_a_list_is_an_error() -> None:
    client, _ = make(ok({"message": "hi"}))
    with pytest.raises(TvmazeError) as error:
        asyncio.run(client.search("x"))
    assert str(error.value) == "the search answer was not a list"


def test_a_search_with_no_matches_is_empty() -> None:
    client, _ = make(ok([]))
    assert asyncio.run(client.search("zzz")) == []


def test_a_search_that_answers_not_found_is_an_error() -> None:
    client, _ = make(FakeResponse(404, None))
    with pytest.raises(TvmazeError):
        asyncio.run(client.search("zzz"))


# ----------------------------------------------------------------------------- schedule


def test_a_schedule_reads_the_show_and_its_next_episode() -> None:
    client, calls = make(ok({**SHOW, "_embedded": {"nextepisode": EPISODE}}))

    found = asyncio.run(client.schedule(44933))

    assert found == Schedule(parse_show(SHOW), parse_episode(EPISODE))
    assert calls[0]["url"] == f"{BASE_URL}/shows/44933"
    assert calls[0]["params"] == {"embed": "nextepisode"}


@pytest.mark.parametrize("embedded", [None, {}, {"nextepisode": None}, "odd"])
def test_a_show_without_a_next_episode_has_none(embedded: Any) -> None:
    client, _ = make(ok({**SHOW, "_embedded": embedded}))
    found = asyncio.run(client.schedule(1))
    assert found == Schedule(parse_show(SHOW), None)


def test_a_next_episode_without_a_date_is_none() -> None:
    client, _ = make(ok({**SHOW, "_embedded": {"nextepisode": {"id": 1, "airstamp": None}}}))
    found = asyncio.run(client.schedule(1))
    assert found is not None
    assert found.next_episode is None


def test_an_unknown_show_is_none_and_a_bad_body_is_an_error() -> None:
    client, _ = make(FakeResponse(404, None), ok(["not", "a", "show"]))
    assert asyncio.run(client.schedule(1)) is None
    with pytest.raises(TvmazeError):
        asyncio.run(client.schedule(2))


def test_schedules_of_different_shows_are_cached_apart() -> None:
    other = {**SHOW, "id": 2, "name": "Other"}
    client, _ = make(ok(SHOW), ok(other))

    first, second = asyncio.run(client.schedule(1)), asyncio.run(client.schedule(2))

    assert first is not None and second is not None
    assert (first.show.name, second.show.name) == ("Severance", "Other")
    assert asyncio.run(client.schedule(1)) == first  # and each is found again under its own key


def test_a_schedule_is_cached_for_a_minute() -> None:
    clock = Clock()
    client, calls = make(ok(SHOW), ok(SHOW), clock=clock)

    asyncio.run(client.schedule(1))
    clock.now = 59.0
    asyncio.run(client.schedule(1))
    assert len(calls) == 1
    clock.now = 60.0
    asyncio.run(client.schedule(1))
    assert len(calls) == 2


# ----------------------------------------------------------------------------- episode


def test_an_episode_lookup_asks_for_that_episode_and_caches_it() -> None:
    clock = Clock()
    client, calls = make(ok(EPISODE), ok(EPISODE), clock=clock)

    first = asyncio.run(client.episode(3804165))
    assert first == parse_episode(EPISODE)
    assert calls[0]["url"] == f"{BASE_URL}/episodes/3804165"
    assert calls[0]["params"] is None
    asyncio.run(client.episode(3804165))
    assert len(calls) == 1
    clock.now = 60.0
    asyncio.run(client.episode(3804165))
    assert len(calls) == 2


def test_episodes_are_cached_apart() -> None:
    client, _ = make(ok(EPISODE), ok({**EPISODE, "id": 2, "name": "Other"}))

    first, second = asyncio.run(client.episode(1)), asyncio.run(client.episode(2))

    assert first is not None and second is not None
    assert (first.title, second.title) == ("Woe's Hollow", "Other")
    assert asyncio.run(client.episode(2)) == second


def test_a_missing_episode_is_none_and_is_not_cached() -> None:
    client, calls = make(FakeResponse(404, None), ok(EPISODE))

    assert asyncio.run(client.episode(1)) is None
    assert asyncio.run(client.episode(1)) == parse_episode(EPISODE)
    assert len(calls) == 2


def test_an_episode_without_a_date_is_none_and_is_not_cached() -> None:
    client, calls = make(ok({"id": 1, "airstamp": None}), ok({"id": 1, "airstamp": None}))

    assert asyncio.run(client.episode(1)) is None
    assert asyncio.run(client.episode(1)) is None
    assert len(calls) == 2


# ----------------------------------------------------------------------------- failures


@pytest.mark.parametrize("status", [429, 500, 503, 403])
def test_a_bad_status_is_an_error_naming_it(status: int) -> None:
    client, _ = make(FakeResponse(status, None))
    with pytest.raises(TvmazeError) as error:
        asyncio.run(client.episode(1))
    assert str(error.value) == f"TVmaze answered HTTP {status}"


def test_a_client_error_is_reported_by_type_only() -> None:
    client, _ = make(aiohttp.ClientConnectionError("secret host detail"))
    with pytest.raises(TvmazeError) as error:
        asyncio.run(client.episode(1))
    assert str(error.value) == "the request failed (ClientConnectionError)"


def test_a_body_that_is_not_json_is_an_error() -> None:
    client, _ = make(ok(ValueError("bad json")))
    with pytest.raises(TvmazeError) as error:
        asyncio.run(client.episode(1))
    assert str(error.value) == "the request failed (ValueError)"


def test_a_slow_answer_times_out() -> None:
    class Slow:
        async def __aenter__(self) -> Slow:
            await asyncio.sleep(1)
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    client, _ = make(Slow(), timeout=0.01)
    with pytest.raises(TvmazeError) as error:
        asyncio.run(client.episode(1))
    assert str(error.value) == "the request timed out"


def test_the_caches_are_bounded_time_limited_caches() -> None:
    client, _ = make()
    for cache in (client._searches, client._schedules, client._episodes):
        assert isinstance(cache, TTLCache)
        assert cache.capacity == 256
    assert client._searches.ttl == 300.0
    assert client._schedules.ttl == 60.0
    assert client._episodes.ttl == 60.0
