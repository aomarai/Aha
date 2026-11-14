"""The PostgreSQL connection pool.

One small pool per process (``DB_POOL_MAX``), every connection carrying a server-side
``statement_timeout`` so a slow query is cancelled by the database instead of outliving Discord's
3-second interaction acknowledgement window and exhausting the pool. Size it so that
``pods x DB_POOL_MAX`` plus the migration and maintenance connections stays below the server's
``max_connections`` (see docs/runbooks/postgres.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

__all__ = ["APPLICATION_NAME", "CONNECT_WAIT", "Database", "connect_once"]

log = logging.getLogger(__name__)


#: Shown in ``pg_stat_activity`` so a DBA can tell the bot's connections apart.
APPLICATION_NAME = "aha"

#: Seconds a starting process keeps trying to reach the database before giving up. A new pod's
#: first packets can be dropped for a second or two while the cluster programs its network policy,
#: and PostgreSQL itself may still be starting; neither is a reason to fail a deploy.
CONNECT_WAIT = 60.0

#: Seconds between attempts while waiting.
_INTERVAL = 2.0

#: Failures that mean "not reachable yet". A wrong password or database is not among them: that
#: would never fix itself, so it fails at once.
_NOT_YET = (OSError, asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError)


async def _retrying[T](
    connect: Callable[[], Awaitable[T]],
    *,
    wait: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """Call ``connect`` until it works, retrying "not reachable yet" errors for ``wait`` seconds."""
    deadline = clock() + wait
    attempt = 0
    while True:
        try:
            return await connect()
        except _NOT_YET as error:
            attempt += 1
            if clock() + _INTERVAL > deadline:
                raise
            # Never the URL (it holds the password): only what kind of failure it was.
            log.warning(
                "Database not reachable yet (attempt %d, %s); retrying in %.0f s",
                attempt,
                type(error).__name__,
                _INTERVAL,
            )
            await sleep(_INTERVAL)


class Database:
    """A pool of connections to the bot's PostgreSQL database."""

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self._pool = pool

    @classmethod
    async def connect(
        cls, url: str, *, max_size: int, statement_timeout: float, wait: float = 0.0
    ) -> Database:
        """Open the pool. ``statement_timeout`` is in seconds and applies to every connection.

        ``wait`` is how many seconds to keep retrying while the database is not reachable yet.
        """

        async def open_pool() -> asyncpg.Pool[Any]:
            return await asyncpg.create_pool(
                url,
                min_size=1,
                max_size=max_size,
                server_settings={
                    "application_name": APPLICATION_NAME,
                    "statement_timeout": str(round(statement_timeout * 1000)),
                },
            )

        return cls(await _retrying(open_pool, wait=wait))

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.pool.PoolConnectionProxy[Any]]:
        """Borrow a connection for the duration of the block."""
        async with self._pool.acquire() as connection:
            yield connection

    async def ping(self) -> bool:
        """Return True if the database answers a trivial query."""
        async with self.acquire() as connection:
            return bool(await connection.fetchval("SELECT 1") == 1)

    async def close(self) -> None:
        """Close the pool, waiting for borrowed connections to be returned."""
        await self._pool.close()


async def connect_once(url: str, *, wait: float = 0.0) -> asyncpg.Connection[Any]:
    """Open a single connection with no statement timeout (for migrations and other jobs).

    ``wait`` is how many seconds to keep retrying while the database is not reachable yet.
    """

    async def open_connection() -> asyncpg.Connection[Any]:
        return await asyncpg.connect(
            url, server_settings={"application_name": f"{APPLICATION_NAME}-job"}
        )

    return await _retrying(open_connection, wait=wait)
