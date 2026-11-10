import asyncio

import pytest
from sqlalchemy import select

from app.db.models import TicketEvent, User
from app.services.tickets import (
    Conflict,
    Forbidden,
    create_ticket,
    reassign_ticket,
    transition_ticket,
)


@pytest.mark.asyncio
async def test_take_checks_version_and_logs_event(sessions):
    async with sessions.begin() as session:
        support = User(telegram_id=1, name="Support", role="support")
        developer = User(telegram_id=2, name="Dev", role="developer")
        session.add_all([support, developer])
        await session.flush()
        ticket = await create_ticket(session, support.id, "Issue", "high", "abc", None)
        ticket_id, developer_id = ticket.id, developer.id
    async with sessions.begin() as session:
        result = await transition_ticket(session, ticket_id, developer_id, "new", 1, "in_progress")
        assert result.version == 2
        assert result.assignee_id == developer_id
        assert result.next_reminder_at is None
    async with sessions.begin() as session:
        with pytest.raises(Conflict):
            await transition_ticket(session, ticket_id, developer_id, "new", 1, "in_progress")


async def test_concurrent_take_and_old_button_after_cycle(sessions):
    async with sessions.begin() as session:
        support = User(telegram_id=1, name="Support", role="support")
        first = User(telegram_id=2, name="First", role="developer")
        second = User(telegram_id=3, name="Second", role="developer")
        session.add_all([support, first, second])
        await session.flush()
        ticket = await create_ticket(session, support.id, "Issue", "high", "abc", None)
        tid, ids = ticket.id, [first.id, second.id]

    async def take(user_id):
        try:
            async with sessions.begin() as session:
                await transition_ticket(session, tid, user_id, "new", 1, "in_progress")
            return user_id
        except Conflict:
            return None

    winners = [x for x in await asyncio.gather(*(take(i) for i in ids)) if x]
    assert len(winners) == 1
    winner = winners[0]
    async with sessions.begin() as session:
        await transition_ticket(session, tid, winner, "in_progress", 2, "waiting_info")
    async with sessions.begin() as session:
        await transition_ticket(session, tid, winner, "waiting_info", 3, "in_progress")
    async with sessions.begin() as session:
        with pytest.raises(Conflict):
            await transition_ticket(session, tid, winner, "in_progress", 2, "resolved")


async def test_reassign_close_and_admin_reopen(sessions):
    async with sessions.begin() as session:
        admin = User(telegram_id=1, name="Admin", role="admin")
        dev = User(telegram_id=2, name="Dev", role="developer")
        session.add_all([admin, dev])
        await session.flush()
        ticket = await create_ticket(session, admin.id, "Issue", "low", "abc", None)
        tid, aid, did = ticket.id, admin.id, dev.id
    async with sessions.begin() as session:
        await transition_ticket(session, tid, aid, "new", 1, "in_progress")
    async with sessions.begin() as session:
        with pytest.raises(Forbidden):
            await reassign_ticket(session, tid, did, did, "in_progress", 2)
        ticket = await reassign_ticket(session, tid, aid, did, "in_progress", 2)
        assert ticket.assignee_id == did and ticket.version == 3
    async with sessions.begin() as session:
        ticket = await transition_ticket(session, tid, did, "in_progress", 3, "resolved")
        assert ticket.closed_at is not None and ticket.version == 4
    async with sessions.begin() as session:
        ticket = await transition_ticket(session, tid, aid, "resolved", 4, "new")
        assert ticket.assignee_id is None and ticket.taken_at is None and ticket.closed_at is None
        assert ticket.next_reminder_at is not None and ticket.version == 5
        events = (
            await session.scalars(
                select(TicketEvent)
                .where(TicketEvent.ticket_id == tid)
                .order_by(TicketEvent.version)
            )
        ).all()
        assert [event.version for event in events] == [1, 2, 3, 4, 5]
