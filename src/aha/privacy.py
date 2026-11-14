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
USER_TABLES = frozenset({"scheduled_jobs", "guild_settings"})

#: Tables the departed-guild purge clears (those with a guild column in the data inventory).
GUILD_TABLES = frozenset({"scheduled_jobs", "guild_settings"})


class _Jobs(Protocol):
    async def purge_user(self, user_id: int) -> int:
        """Delete a user's scheduled jobs."""


class _Settings(Protocol):
    async def anonymise_user(self, user_id: int) -> int:
        """Remove a user's id from server settings."""


@dataclass(frozen=True, slots=True)
class UserPurge:
    """What a deletion removed."""

    #: Reminders and other scheduled jobs deleted.
    jobs: int
    #: Server-settings records whose "last changed by" was your id, now anonymised.
    settings: int


async def purge_user(jobs: _Jobs, settings: _Settings, user_id: int) -> UserPurge:
    """Delete everything stored about ``user_id`` and report how much there was."""
    return UserPurge(
        jobs=await jobs.purge_user(user_id), settings=await settings.anonymise_user(user_id)
    )
