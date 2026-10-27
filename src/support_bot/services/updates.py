"""Committed at-most-one-attempt update claims shared by polling and webhook."""

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.db.models import ProcessedUpdate
from support_bot.observability.metrics import metrics


class UpdateService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], batch_size: int = 100):
        self.sessions = sessions
        self.batch_size = batch_size

    async def claim(self, update_id: int) -> bool:
        async with self.sessions.begin() as session:
            result = await session.scalar(
                insert(ProcessedUpdate)
                .values(update_id=update_id)
                .on_conflict_do_nothing()
                .returning(ProcessedUpdate.update_id)
            )
        claimed = result is not None
        metrics.updates.labels(result="processed" if claimed else "duplicate").inc()
        return claimed

    async def cleanup(self) -> int:
        async with self.sessions.begin() as session:
            expired = (
                select(ProcessedUpdate.update_id)
                .where(ProcessedUpdate.created_at < func.now() - timedelta(hours=48))
                .order_by(ProcessedUpdate.created_at, ProcessedUpdate.update_id)
                .limit(self.batch_size)
                .with_for_update(skip_locked=True)
            )
            deleted = await session.scalars(
                delete(ProcessedUpdate)
                .where(ProcessedUpdate.update_id.in_(expired))
                .returning(ProcessedUpdate.update_id)
            )
            return len(deleted.all())
