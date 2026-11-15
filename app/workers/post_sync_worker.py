"""Background sweeper: publish/update every ticket post that drifted from the DB."""

import asyncio
import contextlib
import logging

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Ticket
from app.services.post_sync import sync_ticket
from app.telegram_gateway import Gateway

logger = logging.getLogger(__name__)


async def pending_ticket_ids(
    session_factory: async_sessionmaker[AsyncSession], *, limit: int = 50
) -> list[int]:
    async with session_factory() as session:
        rows = await session.execute(
            select(Ticket.id)
            .where(
                or_(
                    Ticket.message_id.is_(None),
                    Ticket.post_synced_version < Ticket.version,
                )
            )
            .order_by(Ticket.id)
            .limit(limit)
        )
        return list(rows.scalars().all())


async def sweep_once(
    gateway: Gateway,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    limit: int = 50,
) -> int:
    """Sync all pending tickets; lock-busy failures are skipped, errors logged."""
    ids = await pending_ticket_ids(session_factory, limit=limit)
    synced = 0
    for ticket_id in ids:
        try:
            if await sync_ticket(gateway, session_factory, settings, ticket_id):
                synced += 1
        except Exception:
            logger.exception("sweeper failed for ticket %s", ticket_id)
    return synced


async def run_post_sync_worker(
    gateway: Gateway,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    stop: asyncio.Event,
    *,
    interval_seconds: float | None = None,
) -> None:
    """Loop sweep_once until `stop` is set; first pass runs immediately."""
    interval = interval_seconds or settings.post_sync_interval_seconds
    while not stop.is_set():
        try:
            await sweep_once(gateway, session_factory, settings)
        except Exception:
            logger.exception("post sync sweep crashed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
