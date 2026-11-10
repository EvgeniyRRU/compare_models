import asyncio
from datetime import UTC, datetime, timedelta

from app.db.models import Ticket, User
from app.services.reminders import claim_reminders
from app.services.tickets import INTERVALS, create_ticket


async def test_concurrent_workers_claim_only_once_and_do_not_catch_up(sessions):
    async with sessions.begin() as session:
        user = User(telegram_id=1, name="Support", role="support")
        session.add(user)
        await session.flush()
        ticket = await create_ticket(session, user.id, "Issue", "critical", "A", None)
        ticket.next_reminder_at = datetime.now(UTC) - timedelta(days=2)
        ticket_id = ticket.id
    results = await asyncio.gather(
        claim_reminders(sessions, INTERVALS), claim_reminders(sessions, INTERVALS)
    )
    assert sum(len(batch) for batch in results) == 1
    async with sessions() as session:
        ticket = await session.get(Ticket, ticket_id)
        assert ticket.next_reminder_at > datetime.now(UTC) + timedelta(minutes=9)
        assert ticket.version == 1
