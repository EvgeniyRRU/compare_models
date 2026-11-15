"""Tests for the update-deduplication service (real PostgreSQL)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.services.update_dedup as update_dedup
from app.config import settings
from app.db.models import ProcessedUpdate


async def test_mark_processed_first_then_duplicate(session: AsyncSession) -> None:
    assert await update_dedup.mark_processed(session, 1) is True
    await session.commit()

    assert await update_dedup.mark_processed(session, 1) is False


async def test_mark_processed_duplicate_rejected_across_sessions(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    # Two independent sessions must not both observe the same id as "new".
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as first:
        assert await update_dedup.mark_processed(first, 42) is True
        await first.commit()

    async with factory() as second:
        assert await update_dedup.mark_processed(second, 42) is False


async def test_cleanup_old_updates_deterministic(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the retention so the 48h boundary below is meaningful regardless of env.
    monkeypatch.setattr(settings, "processed_updates_retention", 172800)
    now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=UTC)
    rows = [
        ProcessedUpdate(update_id=1, created_at=now - timedelta(hours=49)),  # old -> deleted
        ProcessedUpdate(update_id=2, created_at=now - timedelta(hours=48)),  # exactly 48h -> kept
        ProcessedUpdate(update_id=3, created_at=now - timedelta(hours=1)),  # recent -> kept
    ]
    session.add_all(rows)
    await session.commit()

    deleted = await update_dedup.cleanup_old_updates(session, now=now)
    await session.commit()

    assert deleted == 1
    remaining = set(await session.scalars(select(ProcessedUpdate.update_id)))
    assert remaining == {2, 3}


async def test_cleanup_old_updates_default_now(session: AsyncSession) -> None:
    old = (
        datetime.now(UTC)
        - timedelta(seconds=settings.processed_updates_retention)
        - timedelta(hours=1)
    )
    session.add(ProcessedUpdate(update_id=1, created_at=old))
    session.add(ProcessedUpdate(update_id=2))  # server_default -> now
    await session.commit()

    deleted = await update_dedup.cleanup_old_updates(session)
    await session.commit()

    assert deleted == 1
    remaining = set(await session.scalars(select(ProcessedUpdate.update_id)))
    assert remaining == {2}


async def test_cleanup_old_updates_uses_configured_retention(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "processed_updates_retention", 60)
    now = datetime.now(UTC)
    session.add_all(
        [
            ProcessedUpdate(update_id=1, created_at=now - timedelta(seconds=100)),  # old -> deleted
            ProcessedUpdate(update_id=2, created_at=now - timedelta(seconds=10)),  # recent -> kept
        ]
    )
    await session.commit()

    deleted = await update_dedup.cleanup_old_updates(session, now=now)
    await session.commit()

    assert deleted == 1
    remaining = set(await session.scalars(select(ProcessedUpdate.update_id)))
    assert remaining == {2}

    # The old hardcoded constant is gone: retention is single-sourced from settings.
    assert not hasattr(update_dedup, "UPDATE_RETENTION")
