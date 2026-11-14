from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete

from support_bot.db.models.ticket_draft import TicketDraft
from support_bot.db.session import SessionFactory
from support_bot.services.tickets import utcnow
from support_bot.services.updates import cleanup_processed_updates


async def cleanup_drafts(session_factory: SessionFactory, ttl_hours: int) -> int:
    cutoff = utcnow() - timedelta(hours=ttl_hours)
    async with session_factory() as session, session.begin():
        result = await session.scalars(
            delete(TicketDraft).where(TicketDraft.updated_at < cutoff).returning(TicketDraft.id)
        )
        return len(list(result.all()))


async def run_cleanup(
    session_factory: SessionFactory,
    *,
    draft_ttl_hours: int,
    updates_retention_hours: int,
) -> tuple[int, int]:
    drafts = await cleanup_drafts(session_factory, draft_ttl_hours)
    updates = await cleanup_processed_updates(session_factory, updates_retention_hours)
    return drafts, updates
