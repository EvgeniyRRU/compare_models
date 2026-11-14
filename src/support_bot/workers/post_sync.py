from __future__ import annotations

import structlog
from sqlalchemy import or_, select

from support_bot.db.models.ticket import Ticket
from support_bot.db.session import SessionFactory
from support_bot.observability.metrics import observe_error
from support_bot.observability.sentry import capture_exception
from support_bot.services.post_sync import PostSyncService


async def post_sync_tick(
    session_factory: SessionFactory,
    service: PostSyncService,
    *,
    batch_size: int,
) -> int:
    logger = structlog.get_logger(__name__)
    async with session_factory() as session:
        ids = list(
            (
                await session.scalars(
                    select(Ticket.id)
                    .where(
                        or_(
                            Ticket.message_id.is_(None),
                            Ticket.post_synced_version < Ticket.version,
                        )
                    )
                    .order_by(Ticket.id)
                    .limit(batch_size)
                )
            ).all()
        )
    synced = 0
    for ticket_id in ids:
        try:
            if await service.sync(ticket_id, try_only=True):
                synced += 1
        except Exception as exc:
            logger.exception("post sync failed", component="post_sync", ticket_id=ticket_id)
            observe_error("post_sync")
            capture_exception(exc, component="post_sync", ticket_id=ticket_id)
    return synced
