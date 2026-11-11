import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.services import drafts, updates
from support_bot.workers import cleanup_job, run_periodic
from tests.helpers import add_user


async def test_periodic_worker_keeps_running_after_a_failed_run() -> None:
    calls = 0
    done = asyncio.Event()

    async def flaky() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        if calls == 3:
            done.set()

    task = asyncio.create_task(run_periodic("test", 0, flaky))
    await asyncio.wait_for(done.wait(), timeout=2)
    task.cancel()

    assert calls >= 3


async def test_cleanup_removes_old_processed_updates_and_stale_drafts(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    now = datetime.now(UTC)
    user = await add_user(session, 4001, "support")
    await updates.register_update(session, 1, now=now - timedelta(hours=49))
    await updates.register_update(session, 2, now=now)
    await drafts.start(session, user, now=now - timedelta(hours=30), ttl_hours=24)
    await session.commit()

    await cleanup_job(session_factory, settings)

    remaining_updates = await session.scalar(text("SELECT count(*) FROM processed_updates"))
    remaining_drafts = await session.scalar(text("SELECT count(*) FROM ticket_drafts"))
    assert remaining_updates == 1
    assert remaining_drafts == 0
