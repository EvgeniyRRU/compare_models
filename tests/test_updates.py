from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.db.models import ProcessedUpdate
from support_bot.services import updates as svc
from tests.helpers import NOW


async def test_first_delivery_is_new_and_repeat_is_duplicate(session: AsyncSession) -> None:
    first = await svc.register_update(session, 1001, now=NOW)
    await session.commit()
    second = await svc.register_update(session, 1001, now=NOW)
    await session.commit()

    assert first is True
    assert second is False


async def test_registration_survives_business_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    calls = 0

    async def crashing_handler() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("handler crashed")

    with pytest.raises(RuntimeError):
        await svc.run_once(session_factory, update_id=2002, now=NOW, handler=crashing_handler)
    handled_again = await svc.run_once(
        session_factory, update_id=2002, now=NOW, handler=crashing_handler
    )

    assert calls == 1
    assert handled_again is False


async def test_old_records_are_purged(session: AsyncSession) -> None:
    await svc.register_update(session, 3001, now=NOW - timedelta(hours=49))
    await svc.register_update(session, 3002, now=NOW - timedelta(hours=1))
    await session.commit()

    removed = await svc.purge_old(session, now=NOW, retention_hours=48)
    await session.commit()

    assert removed == 1
    remaining = (await session.scalars(select(ProcessedUpdate.update_id))).all()
    assert list(remaining) == [3002]
