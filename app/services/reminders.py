from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Ticket


async def claim_reminders(
    sessions: async_sessionmaker, intervals: dict[str, int]
) -> list[tuple[int, int | None]]:
    now = datetime.now(UTC)
    async with sessions.begin() as session:
        tickets = (
            await session.scalars(
                select(Ticket)
                .where(Ticket.status == "new", Ticket.next_reminder_at <= now)
                .order_by(Ticket.next_reminder_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
        claimed = [(ticket.id, ticket.message_id) for ticket in tickets]
        for ticket in tickets:
            ticket.next_reminder_at = now + timedelta(minutes=intervals[ticket.priority])
    return claimed
