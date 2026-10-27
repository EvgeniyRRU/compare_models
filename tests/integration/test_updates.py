import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from support_bot.db.models import ProcessedUpdate
from support_bot.services.updates import UpdateService

pytestmark = pytest.mark.integration


async def test_claim_commits_before_business_error_and_is_concurrent(db):
    service = UpdateService(db.sessions)
    assert sorted(await asyncio.gather(service.claim(123), service.claim(123))) == [False, True]
    with pytest.raises(RuntimeError):
        async with db.sessions.begin():
            raise RuntimeError("business failed")
    assert not await service.claim(123)


async def test_cleanup_keeps_last_48_hours(db):
    async with db.sessions.begin() as session:
        session.add_all(
            [
                ProcessedUpdate(update_id=1, created_at=datetime.now(UTC) - timedelta(hours=49)),
                ProcessedUpdate(update_id=2),
            ]
        )
    await UpdateService(db.sessions).cleanup()
    async with db.sessions() as session:
        assert list(await session.scalars(select(ProcessedUpdate.update_id))) == [2]


async def test_cleanup_is_bounded_per_worker(db):
    async with db.sessions.begin() as session:
        session.add_all(
            [
                ProcessedUpdate(update_id=n, created_at=datetime.now(UTC) - timedelta(hours=49))
                for n in range(1, 4)
            ]
        )
    service = UpdateService(db.sessions, batch_size=1)
    await service.cleanup()
    async with db.sessions() as session:
        assert len(list(await session.scalars(select(ProcessedUpdate.update_id)))) == 2
    await asyncio.gather(service.cleanup(), service.cleanup())
    async with db.sessions() as session:
        assert list(await session.scalars(select(ProcessedUpdate.update_id))) == []
