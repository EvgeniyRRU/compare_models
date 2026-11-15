"""Background loops: post synchronization, reminder dispatch, and cleanup."""

import asyncio
import contextlib
import logging
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models import Ticket
from app.metrics import reminders_sent
from app.services.draft_service import cleanup_expired_drafts
from app.services.post_sync import PostClient, PostRenderer, render_ticket_post, sync_one
from app.services.reminders import ReminderSender, claim_due_reminders, reminder_text
from app.services.update_dedup import cleanup_old_updates

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
        pending = [(t.id, reminder_text(t), t.message_id) for t in due]
        await session.commit()

    sent = 0
    for ticket_id, text, message_id in pending:
        try:
            await send_reminder(text, message_id)
        except Exception:  # noqa: BLE001 — retrying is impossible by design.
            logger.exception("reminder send failed for ticket #%s", ticket_id)
            continue
        sent += 1
        reminders_sent.inc()
    return sent


async def _sleep_interruptibly(seconds: float, stop_event: asyncio.Event) -> None:
    """Sleep up to ``seconds``, returning early once ``stop_event`` is set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)


async def run_post_sync_loop(
    *,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    client: PostClient,
    interval: float,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Drive :func:`sync_pending` on ``interval`` until ``stop_event`` is set."""
    stop = stop_event if stop_event is not None else asyncio.Event()
    while not stop.is_set():
        try:
            await sync_pending(engine=engine, session_factory=session_factory, client=client)
        except Exception:
            logger.exception("post sync pass failed")
        await _sleep_interruptibly(interval, stop)


async def run_reminder_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    send_reminder: ReminderSender,
    interval: float,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Drive :func:`run_reminder_pass` on ``interval`` until ``stop_event`` is set."""
    stop = stop_event if stop_event is not None else asyncio.Event()
    while not stop.is_set():
        try:
            await run_reminder_pass(session_factory=session_factory, send_reminder=send_reminder)
        except Exception:
            logger.exception("reminder pass failed")
        await _sleep_interruptibly(interval, stop)


async def run_draft_cleanup_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    interval: float,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Drive :func:`cleanup_expired_drafts` on ``interval`` until ``stop_event`` is set."""
    stop = stop_event if stop_event is not None else asyncio.Event()
    while not stop.is_set():
        try:
            async with session_factory() as session:
                await cleanup_expired_drafts(session, ttl_seconds=settings.draft_ttl_seconds)
                await session.commit()
        except Exception:
            logger.exception("draft cleanup pass failed")
        await _sleep_interruptibly(interval, stop)


async def run_processed_updates_cleanup_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    interval: float,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Drive :func:`cleanup_old_updates` on ``interval`` until ``stop_event`` is set."""
    stop = stop_event if stop_event is not None else asyncio.Event()
    while not stop.is_set():
        try:
            async with session_factory() as session:
                await cleanup_old_updates(session)
                await session.commit()
        except Exception:
            logger.exception("processed updates cleanup pass failed")
        await _sleep_interruptibly(interval, stop)
