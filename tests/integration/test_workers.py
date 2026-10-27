from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from support_bot.db.models import ProcessedUpdate
from support_bot.services.updates import UpdateService
from support_bot.workers import cleanup

pytestmark = pytest.mark.integration


async def test_retention_drains_backlog_with_bounded_transactions(db, settings):
    settings.worker_batch_size = 2
    async with db.sessions.begin() as session:
        session.add_all(
            [
                ProcessedUpdate(update_id=n, created_at=datetime.now(UTC) - timedelta(hours=49))
                for n in range(7)
            ]
        )
    await cleanup(db, UpdateService(db.sessions, batch_size=2), settings)
    async with db.sessions() as session:
        assert list(await session.scalars(select(ProcessedUpdate.update_id))) == []
