"""Cross-pod IDENTIFY coordination with PostgreSQL advisory locks (#94).

discord.py honours Discord's ``max_concurrency`` (one identify per 5 seconds per bucket, where the
bucket is ``shard_id % max_concurrency``) only inside one process. Several pods starting together
(a rollout, a node reboot) would identify at once and get rate limited or invalidated, so each pod
takes a lock for the shard's bucket before identifying and keeps it for the 5 seconds Discord
requires, whichever pod holds the neighbouring shard.

It fails open: if the database is unreachable or the lock cannot be had in ``timeout`` seconds, the
shard identifies anyway (Discord's own limit is the backstop), because a database outage must not
keep the bot off the gateway.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aha.db import Database

__all__ = ["BASE_LOCK_ID", "IdentifyCoordinator"]

log = logging.getLogger(__name__)

#: First of the advisory-lock keys used for identify buckets (``BASE_LOCK_ID + bucket``). Distinct
#: from the migration lock in :mod:`aha.dbmigrate`.
BASE_LOCK_ID = 7_267_000_100

#: Seconds Discord wants between identifies in one bucket.
HOLD_SECONDS = 5.0

Spawn = Callable[[Callable[[], Awaitable[None]], str], object]


class _Connection(Protocol):
    async def fetchval(self, query: str, *args: Any) -> Any:
        """Run a query and return the first column of the first row."""

    async def execute(self, query: str, *args: Any) -> str:
        """Run a statement."""


class IdentifyCoordinator:
    """Serialises identifies per bucket across every pod sharing the database."""

    def __init__(
        self,
        database: Database,
        spawn: Spawn,
        *,
        hold: float = HOLD_SECONDS,
        poll: float = 0.5,
        timeout: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._db = database
        self._spawn = spawn
        self._hold = hold
        self._poll = poll
        self._timeout = timeout
        self._sleep = sleep
        self._ids = itertools.count(1)

    async def acquire(self, bucket: int) -> None:
        """Return when this pod may identify a shard of ``bucket``.

        The lock stays held for ``hold`` seconds *after* this returns (the identify happens in
        between) and is then released by a background task.
        """
        key = BASE_LOCK_ID + bucket
        try:
            held = self._db.acquire()
            conn = await held.__aenter__()
        except Exception:
            log.warning("Identify coordination unavailable; identifying without it", exc_info=True)
            return
        try:
            locked = await self._lock(conn, key)
        except BaseException:
            await held.__aexit__(None, None, None)
            raise
        if not locked:
            await held.__aexit__(None, None, None)
            log.warning(
                "Could not take the identify lock for bucket %s; identifying anyway", bucket
            )
            return
        number = next(self._ids)

        async def release() -> None:
            try:
                await self._sleep(self._hold)
                await conn.execute("SELECT pg_advisory_unlock($1)", key)
            finally:
                await held.__aexit__(None, None, None)

        self._spawn(release, f"identify-release-{number}")

    async def _lock(self, conn: _Connection, key: int) -> bool:
        """Poll ``pg_try_advisory_lock`` (a blocking lock would trip the statement timeout)."""
        waited = 0.0
        while True:
            try:
                if await conn.fetchval("SELECT pg_try_advisory_lock($1)", key):
                    return True
            except Exception:
                log.warning("Identify lock query failed; identifying without it", exc_info=True)
                return False
            if waited >= self._timeout:
                return False
            await self._sleep(self._poll)
            waited += self._poll
