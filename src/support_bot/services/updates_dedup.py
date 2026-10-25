"""Дедупликация апдейтов Telegram: не более одной попытки обработки каждого update_id."""

from datetime import timedelta

from sqlalchemy import Interval, delete, func, literal
from sqlalchemy.dialects.postgresql import insert as pg_insert

from support_bot.db.engine import SessionFactory
from support_bot.db.models import ProcessedUpdate


class UpdateDeduplicator:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def register(self, update_id: int) -> bool:
        """Записать update_id отдельной короткой транзакцией. False — апдейт уже был."""
        async with self._sf.begin() as session:
            result = await session.execute(
                pg_insert(ProcessedUpdate)
                .values(update_id=update_id)
                .on_conflict_do_nothing(index_elements=[ProcessedUpdate.update_id])
                .returning(ProcessedUpdate.update_id)
            )
            return result.first() is not None

    async def delete_older_than(self, ttl: timedelta) -> int:
        async with self._sf.begin() as session:
            result = await session.execute(
                delete(ProcessedUpdate)
                .where(ProcessedUpdate.processed_at < func.now() - literal(ttl, Interval()))
                .returning(ProcessedUpdate.update_id)
            )
            return len(result.all())
