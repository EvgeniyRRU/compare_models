"""Background loop for synchronizing ticket posts to the dev chat."""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import Ticket
from app.services.post_sync import PostClient, PostRenderer, _sync_one, render_ticket_post


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
        did_sync = await _sync_one(
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
