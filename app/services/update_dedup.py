"""Update-deduplication service: the ``processed_updates`` ledger.

Telegram redelivers updates on failure, so each ``update_id`` must be processed
at most once. ``mark_processed`` is the idempotent, concurrency-safe gate;
``cleanup_old_updates`` prunes the ledger to bound its size. The caller owns the
transaction — neither function commits.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import ProcessedUpdate


async def mark_processed(session: AsyncSession, update_id: int) -> bool:
    """Record ``update_id``; return ``True`` only if it was newly seen.

    Idempotent and race-safe via ``INSERT ... ON CONFLICT DO NOTHING`` against
    the ``update_id`` primary key: exactly one concurrent caller observes the
    row as inserted (``returning`` yields a row) and everyone else gets ``None``.
    """
    stmt = (
        pg_insert(ProcessedUpdate)
        .values(update_id=update_id)
        .on_conflict_do_nothing(index_elements=[ProcessedUpdate.update_id])
        .returning(ProcessedUpdate.update_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def cleanup_old_updates(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Delete ledger rows older than ``settings.processed_updates_retention``; return the count."""
    if now is None:
        now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.processed_updates_retention)
    result = cast(
        CursorResult[Any],
        await session.execute(delete(ProcessedUpdate).where(ProcessedUpdate.created_at < cutoff)),
    )
    return result.rowcount
