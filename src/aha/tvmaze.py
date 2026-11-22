"""A small client for the TVmaze public API, behind ``/watch``: find a show, read its next episode.

TVmaze needs no key. It asks for attribution (CC BY-SA 4.0, shown in the command replies and in
docs/privacy.md) and allows 20 calls per 10 seconds per address, so answers are cached briefly and a
``429`` is reported like any other temporary failure. Nothing about the Discord user is sent: only
the show name or id. Everything that comes back is untrusted data: it is parsed defensively,
clipped, and shown with :func:`aha.textutil.plain`. Failures raise :class:`TvmazeError` with a
short reason that is safe to log.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from datetime import time as clock_time
from typing import Any

import aiohttp

from aha.net import TTLCache
from aha.textutil import clip

__all__ = [
    "BASE_URL",
    "MAX_RESULTS",
    "Episode",
    "Schedule",
    "Show",
    "TvmazeClient",
    "TvmazeError",
]

BASE_URL = "https://api.tvmaze.com"

#: Identifies this client without naming any person (TVmaze asks API users to).
USER_AGENT = "Aha-Discord-Bot (+https://github.com/aomarai/Aha)"

#: Seconds before one request is abandoned. The scheduler gives a job 30 s, so a handler can make a
#: few calls.
TIMEOUT = 6.0

#: Seconds a search is remembered, and a show or episode lookup. Lookups are short so a delayed
#: episode is noticed, but long enough that many followers of one show cost one request.
SEARCH_TTL = 300.0
LOOKUP_TTL = 60.0
CACHE_SIZE = 256

#: Most search results kept (Discord shows at most 25 autocomplete choices).
MAX_RESULTS = 10

#: Longest show or episode name kept.
NAME_LIMIT = 100

#: The time of day assumed for an episode that only has a date.
_DATE_ONLY_TIME = clock_time(12, 0)


class TvmazeError(Exception):
    """TVmaze could not be asked, or answered in a way that cannot be used."""


@dataclass(frozen=True, slots=True)
class Show:
    """A show, as much as ``/watch`` needs."""

    id: int
    name: str
    #: ``Running``, ``Ended``, ``To Be Determined`` or ``In Development``.
    status: str
    #: Year of the first episode, when known.
    year: int | None
    #: The network or streaming service, when known.
    network: str | None

    @property
    def ended(self) -> bool:
        """Whether the show has finished for good."""
        return self.status == "Ended"


@dataclass(frozen=True, slots=True)
class Episode:
    """An episode and when it airs."""

    id: int
    title: str
    season: int | None
    number: int | None
    #: When it airs, in UTC.
    airs_at: datetime
    #: ``True`` when TVmaze only knows the date, so ``airs_at`` is a guess within that day.
    approximate: bool = False

    @property
    def code(self) -> str | None:
        """``S2E04`` style label, or ``None`` for a special without numbers."""
        if self.season is None or self.number is None:
            return None
        return f"S{self.season}E{self.number:02d}"


@dataclass(frozen=True, slots=True)
class Schedule:
    """A show with its next episode (``None`` while none is announced)."""

    show: Show
    next_episode: Episode | None


def _optional_int(value: Any) -> int | None:
    """``int(value)`` for a number TVmaze may omit."""
    return None if value is None else int(value)


def parse_show(raw: Any) -> Show:
    """Build a :class:`Show` from TVmaze JSON, or raise :class:`TvmazeError`."""
    try:
        name = clip(str(raw["name"]).strip(), NAME_LIMIT)
        if not name:
            raise TvmazeError("a show has no name")
        premiered = raw.get("premiered")
        network = (raw.get("network") or raw.get("webChannel") or {}).get("name")
        return Show(
            id=int(raw["id"]),
            name=name,
            status=str(raw.get("status") or ""),
            year=int(str(premiered)[:4]) if premiered else None,
            network=clip(str(network), NAME_LIMIT) if network else None,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise TvmazeError("a show was not in the expected shape") from exc


def parse_episode(raw: Any) -> Episode | None:
    """Build an :class:`Episode`; ``None`` when TVmaze has no date for it. Raises on bad shapes."""
    try:
        stamp = raw.get("airstamp")
        approximate = not stamp
        if stamp:
            parsed = datetime.fromisoformat(str(stamp))
            airs_at = (
                parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
            )
        elif raw.get("airdate"):
            airs_at = datetime.combine(
                date.fromisoformat(str(raw["airdate"])), _DATE_ONLY_TIME, tzinfo=UTC
            )
        else:
            return None
        return Episode(
            id=int(raw["id"]),
            title=clip(str(raw.get("name") or "").strip(), NAME_LIMIT),
            season=_optional_int(raw.get("season")),
            number=_optional_int(raw.get("number")),
            airs_at=airs_at,
            approximate=approximate,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise TvmazeError("an episode was not in the expected shape") from exc


class TvmazeClient:
    """Asks TVmaze, with short caches."""

    def __init__(
        self,
        *,
        session_factory: Callable[..., aiohttp.ClientSession] = aiohttp.ClientSession,
        base_url: str = BASE_URL,
        timeout: float = TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._base_url = base_url
        self._timeout = timeout
        self._searches: TTLCache[list[Show]] = TTLCache(SEARCH_TTL, CACHE_SIZE, clock)
        self._schedules: TTLCache[Schedule] = TTLCache(LOOKUP_TTL, CACHE_SIZE, clock)
        self._episodes: TTLCache[Episode] = TTLCache(LOOKUP_TTL, CACHE_SIZE, clock)

    async def search(self, query: str) -> list[Show]:
        """Shows matching ``query``, best first (empty for a blank query or no match)."""
        key = " ".join(query.lower().split())
        if not key:
            return []
        cached = self._searches.get(key)
        if cached is not None:
            return cached
        raw = await self._get("/search/shows", {"q": key})
        if not isinstance(raw, list):
            raise TvmazeError("the search answer was not a list")
        shows: list[Show] = []
        for item in raw:
            try:
                shows.append(parse_show(item["show"]))
            except KeyError, TypeError, TvmazeError:
                continue  # one odd result must not hide the others
        shows = shows[:MAX_RESULTS]
        self._searches.put(key, shows)
        return shows

    async def schedule(self, show_id: int) -> Schedule | None:
        """A show and its next episode; ``None`` when TVmaze has no such show."""
        key = str(show_id)
        cached = self._schedules.get(key)
        if cached is not None:
            return cached
        raw = await self._get(f"/shows/{show_id}", {"embed": "nextepisode"})
        if raw is None:
            return None
        embedded = raw.get("_embedded") if isinstance(raw, dict) else None
        upcoming = embedded.get("nextepisode") if isinstance(embedded, dict) else None
        found = Schedule(
            show=parse_show(raw),
            next_episode=parse_episode(upcoming) if upcoming else None,
        )
        self._schedules.put(key, found)
        return found

    async def episode(self, episode_id: int) -> Episode | None:
        """One episode with its current air time; ``None`` when it is gone or has no date."""
        key = str(episode_id)
        cached = self._episodes.get(key)
        if cached is not None:
            return cached
        raw = await self._get(f"/episodes/{episode_id}", None)
        if raw is None:
            return None
        found = parse_episode(raw)
        if found is not None:
            self._episodes.put(key, found)
        return found

    async def _get(self, path: str, params: dict[str, str] | None) -> Any:
        """GET ``path`` and return its JSON, or ``None`` for a 404."""
        try:
            async with (
                asyncio.timeout(self._timeout),
                self._session_factory(headers={"User-Agent": USER_AGENT}) as session,
                session.get(f"{self._base_url}{path}", params=params) as response,
            ):
                if response.status == 404:
                    return None
                if response.status != 200:
                    raise TvmazeError(f"TVmaze answered HTTP {response.status}")
                return await response.json()
        except TimeoutError as exc:
            raise TvmazeError("the request timed out") from exc
        except (aiohttp.ClientError, ValueError) as exc:
            raise TvmazeError(f"the request failed ({type(exc).__name__})") from exc
