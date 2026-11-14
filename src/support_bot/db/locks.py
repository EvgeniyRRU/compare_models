from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from support_bot.domain.errors import LockUnavailable


@asynccontextmanager
async def advisory_lock(
    dsn: str,
    key: int,
    *,
    wait_timeout: float,
    try_only: bool = False,
    poll_interval: float = 0.1,
) -> AsyncIterator[None]:
    """
    Session-level PostgreSQL advisory lock on a dedicated physical connection.

    Closing the connection always releases the lock, so a failed unlock cannot
    leak a lock back into a pool.
    """
    connection = await asyncpg.connect(dsn=dsn)
    acquired = False
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_timeout
        while True:
            acquired = bool(await connection.fetchval("SELECT pg_try_advisory_lock($1)", key))
            if acquired:
                break
            if try_only or loop.time() >= deadline:
                raise LockUnavailable(f"advisory lock {key} is busy")
            await asyncio.sleep(poll_interval)
        yield
    finally:
        if acquired:
            try:
                await asyncio.shield(connection.execute("SELECT pg_advisory_unlock($1)", key))
            except BaseException:
                connection.terminate()
        try:
            await asyncio.shield(connection.close())
        except BaseException:
            connection.terminate()
