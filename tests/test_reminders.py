import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update

from app.db.models import Priority, Ticket, TicketEvent, User
from app.db.session import SessionLocal
from app.services.reminders import claim_due_reminders
from app.services.tickets import create_ticket


@pytest.mark.asyncio
async def test_concurrent_reminder_workers_claim_due_ticket_once():
    async with SessionLocal.begin() as session:
        telegram_id = int(time.time() * 1000000)
        user = User(telegram_id=telegram_id, role="support", full_name="Reminder test")
        session.add(user)
        await session.flush()
        ticket = await create_ticket(
            session,
            author=user,
            description="Due",
            priority=Priority.CRITICAL,
            request_id=f"REM-{telegram_id}",
            tenant_id=None,
        )
        ticket_id, user_id = ticket.id, user.id
        await session.execute(
            update(Ticket)
            .where(Ticket.id == ticket_id)
            .values(next_reminder_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    try:
        first, second = await asyncio.gather(claim_due_reminders(), claim_due_reminders())
        claimed = first + second
        assert claimed.count((ticket_id, None)) == 1
        assert all(ticket_id != claimed_id for claimed_id, _ in await claim_due_reminders())
        async with SessionLocal() as session:
            ticket = await session.get(Ticket, ticket_id)
            assert ticket.version == 1
            assert ticket.next_reminder_at > datetime.now(UTC)
    finally:
        async with SessionLocal.begin() as session:
            await session.execute(delete(TicketEvent).where(TicketEvent.ticket_id == ticket_id))
            await session.execute(delete(Ticket).where(Ticket.id == ticket_id))
            await session.execute(delete(User).where(User.id == user_id))
