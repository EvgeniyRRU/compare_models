from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from support_bot.db.models import Ticket, User

pytestmark = pytest.mark.integration


async def test_constraints_and_technical_timestamp(db):
    async with db.sessions.begin() as session:
        user = User(telegram_id=1, first_name="Test", role="admin")
        session.add(user)
        await session.flush()
        ticket = Ticket(
            author_id=user.id,
            description="Test",
            request_id="R",
            priority="high",
            next_reminder_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        session.add(ticket)
        await session.flush()
        ticket_id = ticket.id
        old_updated = ticket.updated_at
    async with db.sessions.begin() as session:
        await session.execute(
            text("UPDATE tickets SET message_id=123, post_synced_version=1 WHERE id=:id"),
            {"id": ticket_id},
        )
    async with db.sessions() as session:
        saved = await session.get(Ticket, ticket_id)
        assert saved.version == 1
        assert saved.updated_at > old_updated
    with pytest.raises(IntegrityError):
        async with db.sessions.begin() as session:
            await session.execute(
                text("UPDATE tickets SET status='resolved' WHERE id=:id"), {"id": ticket_id}
            )
