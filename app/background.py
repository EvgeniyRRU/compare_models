"""Background loops: post synchronization and reminder dispatch."""

import logging
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import Ticket
from app.services.post_sync import PostClient, PostRenderer, render_ticket_post, sync_one
from app.services.reminders import ReminderSender, claim_due_reminders, reminder_text

logger = logging.getLogger(__name__)


async def find_unsynced_ticket_ids(session: AsyncSession, *, limit: int = 100) -> list[int]:
    """Return ids of tickets whose post is missing or stale, ordered by id."""
    stmt = (
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
    result = await session.execute(stmt)
    return list(result.scalars())


async def sync_pending(
    *,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    client: PostClient,
    render: PostRenderer = render_ticket_post,
    limit: int = 100,
) -> int:
    """One background pass: find unsynced tickets, try-lock + sync each.

    Each ticket is synced with a non-blocking advisory lock, so a ticket already
    being synced by another worker is skipped rather than blocked on. Returns
    the number of tickets actually synced.
    """
    async with session_factory() as session:
        ticket_ids = await find_unsynced_ticket_ids(session, limit=limit)

    synced = 0
    for ticket_id in ticket_ids:
        did_sync = await sync_one(
            engine=engine,
            session_factory=session_factory,
            ticket_id=ticket_id,
            client=client,
            render=render,
            blocking=False,
        )
        if did_sync:
            synced += 1
    return synced


async def run_reminder_pass(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    send_reminder: ReminderSender,
    now: datetime | None = None,
) -> int:
    """One reminder pass: claim due reminders (commit), then send each.

    Sends happen AFTER commit. A send error is logged (Sentry in Stage 8) and
    the reminder is NOT retried (the claim already advanced next_reminder_at).
    Returns the number of reminders successfully sent.
    """
    async with session_factory() as session:
        due = await claim_due_reminders(session, now=now)
        await session.commit()

    sent = 0
    for ticket in due:
        try:
            await send_reminder(reminder_text(ticket), ticket.message_id)
        except Exception:  # noqa: BLE001 — retrying is impossible by design.
            logger.exception("reminder send failed for ticket #%s", ticket.id)
            continue
        sent += 1
    return sent
