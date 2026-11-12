from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.core.config import get_settings
from app.db.models import ProcessedUpdate, TicketDraft
from app.db.session import SessionLocal


async def purge_expired_state() -> tuple[int, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    async with SessionLocal.begin() as session:
        drafts = await session.execute(
            delete(TicketDraft).where(
                TicketDraft.updated_at < now - timedelta(hours=settings.draft_ttl_hours)
            )
        )
        updates = await session.execute(
            delete(ProcessedUpdate).where(
                ProcessedUpdate.created_at < now - timedelta(hours=settings.update_retention_hours)
            )
        )
    return getattr(drafts, "rowcount", 0) or 0, getattr(updates, "rowcount", 0) or 0
