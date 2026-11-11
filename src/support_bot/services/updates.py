from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.db.models import ProcessedUpdate


async def register_update(session: AsyncSession, update_id: int, *, now: datetime) -> bool:
    """Record the update id. Returns False if it was already recorded.

    The caller commits this in its own short transaction before business handling.
    """
    inserted = await session.scalar(
        insert(ProcessedUpdate)
        .values(update_id=update_id, created_at=now)
        .on_conflict_do_nothing(index_elements=[ProcessedUpdate.update_id])
        .returning(ProcessedUpdate.update_id)
    )
    return inserted is not None


async def purge_old(session: AsyncSession, *, now: datetime, retention_hours: int) -> int:
    result = await session.execute(
        delete(ProcessedUpdate).where(
            ProcessedUpdate.created_at < now - timedelta(hours=retention_hours)
        )
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


async def run_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    update_id: int,
    now: datetime,
    handler: Callable[[], Awaitable[None]],
) -> bool:
    """Process an update at most once. Returns False for a duplicate.

    The registration is committed before the handler runs, so a handler failure
    leaves the update marked as processed (no second attempt).
    """
    async with session_factory.begin() as session:
        is_new = await register_update(session, update_id, now=now)
    if not is_new:
        return False
    await handler()
    return True
