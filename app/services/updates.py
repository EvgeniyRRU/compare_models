from sqlalchemy.dialects.postgresql import insert

from app.db.models import ProcessedUpdate
from app.db.session import SessionLocal


async def claim_update(update_id: int) -> bool:
    async with SessionLocal.begin() as session:
        result = await session.execute(
            insert(ProcessedUpdate)
            .values(update_id=update_id)
            .on_conflict_do_nothing()
            .returning(ProcessedUpdate.update_id)
        )
        return result.scalar_one_or_none() is not None
