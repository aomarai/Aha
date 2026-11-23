"""Deleting a person's data (#99): what ``/forgetme`` removes, and the guard that keeps it complete.

Every table in :data:`aha.datainventory.INVENTORY` that has a user column must be purged here, and a
test fails when a migration adds one that is not listed in :data:`USER_TABLES`, so a new feature
cannot ship with data that cannot be deleted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["GUILD_TABLES", "USER_TABLES", "UserPurge", "purge_user"]

#: Tables :func:`purge_user` clears (those with a user column in the data inventory).
USER_TABLES = frozenset(
    {"scheduled_jobs", "guild_settings", "summarize_usage", "show_subscriptions", "voice_queue"}
)

#: Tables the departed-guild purge clears (those with a guild column in the data inventory).
GUILD_TABLES = frozenset({"scheduled_jobs", "guild_settings", "show_subscriptions", "voice_queue"})


class _Jobs(Protocol):
    async def purge_user(self, user_id: int) -> int:
        """Delete a user's scheduled jobs, except those a show subscription owns."""


class _Settings(Protocol):
    async def anonymise_user(self, user_id: int) -> int:
        """Remove a user's id from server settings."""


class _Usage(Protocol):
    async def purge_user(self, user_id: int) -> int:
        """Delete a user's summary counters."""


class _Shows(Protocol):
    async def purge_user(self, user_id: int) -> int:
        """Delete a user's show subscriptions (and the jobs they own)."""


class _Queue(Protocol):
    async def purge_user(self, user_id: int) -> int:
        """Delete the tracks a user queued."""


@dataclass(frozen=True, slots=True)
class UserPurge:
    """What a deletion removed."""

    #: Reminders and other scheduled jobs deleted.
    jobs: int
    #: Server-settings records whose "last changed by" was your id, now anonymised.
    settings: int
    #: Daily ``/summarize`` counters deleted.
    usage: int
    #: Show subscriptions (``/watch``) deleted.
    shows: int
    #: Voice queue tracks (``/play``) deleted.
    tracks: int


async def purge_user(
    jobs: _Jobs, settings: _Settings, usage: _Usage, shows: _Shows, queue: _Queue, user_id: int
) -> UserPurge:
    """Delete everything stored about ``user_id`` and report how much there was.

    Subscriptions go last: the job purge leaves the jobs they own, and a ``/watch`` that commits
    while this runs is still removed because its subscription is deleted after it.
    """
    return UserPurge(
        jobs=await jobs.purge_user(user_id),
        settings=await settings.anonymise_user(user_id),
        usage=await usage.purge_user(user_id),
        shows=await shows.purge_user(user_id),
        tracks=await queue.purge_user(user_id),
    )
