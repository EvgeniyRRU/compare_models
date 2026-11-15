"""Telegram update deduplication.

`reserve` runs in its own short transaction BEFORE any business processing: the row
survives a failed/skipped handler, so Telegram retries of the same update_id never
re-execute business logic (at-most-once processing; a handler crash is not retried
indefinitely — errors go to Sentry, the update is dropped).
"""

from typing import Any, cast

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ProcessedUpdate
from app.logging_setup import get_logger

log = get_logger("services.dedup")


async def reserve(
    session_factory: async_sessionmaker[AsyncSession], update_id: int
) -> bool:
    """INSERT ON CONFLICT DO NOTHING; True when this caller got the update."""
    async with session_factory() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                pg_insert(ProcessedUpdate)
                .values(update_id=update_id)
                .on_conflict_do_nothing(index_elements=["update_id"])
            ),
        )
    return result.rowcount > 0


async def last_update_id(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return (
            await session.scalar(text("SELECT COALESCE(MAX(update_id), 0) FROM processed_updates"))
            or 0
        )


async def cleanup(session_factory: async_sessionmaker[AsyncSession], max_age_hours: float) -> int:
    """Delete processed rows older than the TTL; returns how many."""
    async with session_factory() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                delete(ProcessedUpdate).where(
                    ProcessedUpdate.created_at
                    < text("now() - make_interval(hours => :hours)").bindparams(
                        hours=int(max_age_hours)
                    )
                )
            ),
        )
    if result.rowcount:
        log.info("dedup_cleanup", component="dedup", removed=result.rowcount)
    return result.rowcount
