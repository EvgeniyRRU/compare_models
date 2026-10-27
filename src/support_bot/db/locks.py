"""Session advisory locks stay on a dedicated physical PostgreSQL connection."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


async def _release(connection: AsyncConnection, ticket_id: int) -> None:
    try:
        if connection.in_transaction():
            await connection.rollback()
        await connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": ticket_id})
        await connection.commit()
    except BaseException:
        # A connection with an uncertain session lock must never return to the pool.
        await connection.invalidate()
        raise


@asynccontextmanager
async def ticket_lock(
    engine: AsyncEngine, ticket_id: int, lock_timeout: float, wait: bool = True
) -> AsyncIterator[AsyncConnection | None]:
    async with engine.connect() as connection:
        attempted = False
        try:
            async with asyncio.timeout(lock_timeout):
                while True:
                    attempted = True
                    acquired = bool(
                        await connection.scalar(
                            text("SELECT pg_try_advisory_lock(:id)"), {"id": ticket_id}
                        )
                    )
                    attempted = acquired
                    await connection.commit()
                    if acquired or not wait:
                        break
                    await asyncio.sleep(min(0.05, lock_timeout))
            yield connection if acquired else None
        finally:
            if attempted:
                cleanup = asyncio.create_task(_release(connection, ticket_id))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # Finish explicit unlock before the outer context returns the connection.
                    try:
                        await cleanup
                    except BaseException:
                        await connection.invalidate()
                    raise
