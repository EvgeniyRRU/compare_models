import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from telegram.error import NetworkError

from support_bot.db.models import Ticket, TicketEvent, User
from support_bot.services.reminders import ReminderService

pytestmark = pytest.mark.integration


async def due_ticket(db):
    async with db.sessions.begin() as session:
        user = User(telegram_id=1, first_name="Test", role="support")
        session.add(user)
        await session.flush()
        ticket = Ticket(
            author_id=user.id,
            description="Test",
            request_id="R",
            priority="high",
            next_reminder_at=datetime.now(UTC) - timedelta(days=5),
        )
        session.add(ticket)
        await session.flush()
        return ticket.id


async def test_concurrent_claim_advances_from_now_without_business_change(db, settings):
    ticket_id = await due_ticket(db)
    service = ReminderService(db.sessions, settings, AsyncMock())
    claims = await asyncio.gather(service.claim(), service.claim())
    assert sorted(claims, key=len) == [[], [(ticket_id, None)]]
    async with db.sessions() as session:
        ticket = await session.get(Ticket, ticket_id)
        assert ticket.version == 1
        assert (
            abs(
                (ticket.next_reminder_at - datetime.now(UTC)).total_seconds()
                - settings.reminder_high_seconds
            )
            < 3
        )
        assert await session.scalar(select(func.count()).select_from(TicketEvent)) == 0
    assert await service.claim() == []


async def test_send_failure_is_one_attempt_and_deadline_already_committed(db, settings):
    ticket_id = await due_ticket(db)
    transport = AsyncMock()
    transport.send_reminder.side_effect = NetworkError("offline")
    service = ReminderService(db.sessions, settings, transport)
    await service.run_once()
    await service.run_once()
    transport.send_reminder.assert_awaited_once_with(ticket_id, None)
