import time

import pytest
from sqlalchemy import delete

from app.db.models import ProcessedUpdate
from app.db.session import SessionLocal
from app.services.updates import claim_update


@pytest.mark.asyncio
async def test_update_is_committed_once_before_business_work():
    update_id = time.time_ns()
    try:
        assert await claim_update(update_id)
        # Business work uses another transaction; its failure leaves this claim committed.
        with pytest.raises(RuntimeError, match="business failure"):
            raise RuntimeError("business failure")
        assert not await claim_update(update_id)
    finally:
        async with SessionLocal.begin() as session:
            await session.execute(
                delete(ProcessedUpdate).where(ProcessedUpdate.update_id == update_id)
            )
