"""Tests for the update-deduplication service (real PostgreSQL)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import ProcessedUpdate
from app.services.update_dedup import UPDATE_RETENTION, cleanup_old_updates, mark_processed


async def test_mark_processed_first_then_duplicate(session: AsyncSession) -> None:
    assert await mark_processed(session, 1) is True
    await session.commit()

    assert await mark_processed(session, 1) is False


async def test_mark_processed_duplicate_rejected_across_sessions(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    # Two independent sessions must not both observe the same id as "new".
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as first:
        assert await mark_processed(first, 42) is True
        await first.commit()

    async with factory() as second:
        assert await mark_processed(second, 42) is False


async def test_cleanup_old_updates_deterministic(session: AsyncSession) -> None:
    now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)
    rows = [
        ProcessedUpdate(update_id=1, created_at=now - timedelta(hours=49)),  # old -> deleted
        ProcessedUpdate(update_id=2, created_at=now - timedelta(hours=48)),  # exactly 48h -> kept
        ProcessedUpdate(update_id=3, created_at=now - timedelta(hours=1)),  # recent -> kept
    ]
    session.add_all(rows)
    await session.commit()

    deleted = await cleanup_old_updates(session, now=now)
    await session.commit()

    assert deleted == 1
    remaining = set(await session.scalars(select(ProcessedUpdate.update_id)))
    assert remaining == {2, 3}


async def test_cleanup_old_updates_default_now(session: AsyncSession) -> None:
    old = datetime.now(UTC) - UPDATE_RETENTION - timedelta(hours=1)
    session.add(ProcessedUpdate(update_id=1, created_at=old))
    session.add(ProcessedUpdate(update_id=2))  # server_default -> now
    await session.commit()

    deleted = await cleanup_old_updates(session)
    await session.commit()

    assert deleted == 1
    remaining = set(await session.scalars(select(ProcessedUpdate.update_id)))
    assert remaining == {2}
