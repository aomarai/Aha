"""The PostgreSQL connection pool.

One small pool per process (``DB_POOL_MAX``), every connection carrying a server-side
``statement_timeout`` so a slow query is cancelled by the database instead of outliving Discord's
3-second interaction acknowledgement window and exhausting the pool. Size it so that
``pods x DB_POOL_MAX`` plus the migration and maintenance connections stays below the server's
``max_connections`` (see docs/runbooks/postgres.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg

__all__ = ["APPLICATION_NAME", "Database", "connect_once"]

#: Shown in ``pg_stat_activity`` so a DBA can tell the bot's connections apart.
APPLICATION_NAME = "aha"


class Database:
    """A pool of connections to the bot's PostgreSQL database."""

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, url: str, *, max_size: int, statement_timeout: float) -> Database:
        """Open the pool. ``statement_timeout`` is in seconds and applies to every connection."""
        pool: asyncpg.Pool[Any] = await asyncpg.create_pool(
            url,
            min_size=1,
            max_size=max_size,
            server_settings={
                "application_name": APPLICATION_NAME,
                "statement_timeout": str(round(statement_timeout * 1000)),
            },
        )
        return cls(pool)

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


async def connect_once(url: str) -> asyncpg.Connection[Any]:
    """Open a single connection with no statement timeout (for migrations and other jobs)."""
    return await asyncpg.connect(
        url, server_settings={"application_name": f"{APPLICATION_NAME}-job"}
    )
