from datetime import timedelta

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ProcessedUpdate


async def claim_update(sessionmaker: async_sessionmaker[AsyncSession], update_id: int) -> bool:
    """Отдельная короткая транзакция до бизнес-обработки. False — апдейт уже встречался."""
    async with sessionmaker() as session, session.begin():
        res = await session.execute(
            insert(ProcessedUpdate)
            .values(update_id=update_id)
            .on_conflict_do_nothing()
            .returning(ProcessedUpdate.update_id)
        )
        return res.scalar_one_or_none() is not None


async def purge_old(session: AsyncSession, ttl: timedelta) -> int:
    res = await session.execute(
        delete(ProcessedUpdate).where(ProcessedUpdate.created_at < func.now() - ttl)
    )
    return res.rowcount  # type: ignore[attr-defined,no-any-return]
