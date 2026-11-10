import asyncio

import pytest
from sqlalchemy import select

from app.db.models import User
from app.services.tickets import create_ticket, transition_ticket
from app.services.users import ActiveTickets, set_role


async def test_cannot_remove_assignee_rights_with_active_ticket(sessions):
    async with sessions.begin() as session:
        admin = User(telegram_id=1, name="Admin", role="admin")
        dev = User(telegram_id=2, name="Dev", role="developer")
        session.add_all([admin, dev])
        await session.flush()
        ticket = await create_ticket(session, admin.id, "Issue", "high", "A", None)
        ticket_id, admin_id, dev_id = ticket.id, admin.id, dev.id
        await transition_ticket(session, ticket_id, dev_id, "new", 1, "in_progress")
    async with sessions.begin() as session:
        with pytest.raises(ActiveTickets) as exc:
            await set_role(session, admin_id, dev_id, "support")
        assert exc.value.ticket_ids == [ticket_id]


async def test_role_change_waits_for_inflight_take(sessions):
    async with sessions.begin() as session:
        admin = User(telegram_id=1, name="Admin", role="admin")
        dev = User(telegram_id=2, name="Dev", role="developer")
        session.add_all([admin, dev])
        await session.flush()
        ticket = await create_ticket(session, admin.id, "Issue", "high", "A", None)
        ticket_id, admin_id, dev_id = ticket.id, admin.id, dev.id
    async with sessions.begin() as session:
        await session.scalar(select(User).where(User.id == dev_id).with_for_update(read=True))

        async def demote():
            async with sessions.begin() as other:
                await set_role(other, admin_id, dev_id, "support")

        task = asyncio.create_task(demote())
        await asyncio.sleep(0.05)
        assert not task.done()
        await transition_ticket(session, ticket_id, dev_id, "new", 1, "in_progress")
    with pytest.raises(ActiveTickets):
        await task
