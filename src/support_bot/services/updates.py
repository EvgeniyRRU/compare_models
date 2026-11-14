from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert

from support_bot.db.models.processed_update import ProcessedUpdate
from support_bot.db.session import SessionFactory
from support_bot.services.tickets import utcnow


async def try_mark_processed(session_factory: SessionFactory, update_id: int) -> bool:
    """Insert update_id in its own committed transaction. False means duplicate."""
    async with session_factory() as session, session.begin():
        stmt = (
            pg_insert(ProcessedUpdate)
            .values(update_id=update_id)
            .on_conflict_do_nothing(index_elements=[ProcessedUpdate.update_id])
            .returning(ProcessedUpdate.update_id)
        )
        return (await session.scalar(stmt)) is not None


async def cleanup_processed_updates(session_factory: SessionFactory, retention_hours: int) -> int:
    cutoff = utcnow() - timedelta(hours=retention_hours)
    async with session_factory() as session, session.begin():
        result = await session.scalars(
            delete(ProcessedUpdate)
            .where(ProcessedUpdate.created_at < cutoff)
            .returning(ProcessedUpdate.update_id)
        )
        return len(list(result.all()))
