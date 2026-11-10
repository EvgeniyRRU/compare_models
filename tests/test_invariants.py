import pytest
from sqlalchemy.exc import IntegrityError

from app.db.models import Ticket, User


@pytest.mark.parametrize(
    ("status", "assignee", "taken", "closed"),
    [
        ("new", True, False, False),
        ("in_progress", False, False, False),
        ("waiting_info", False, True, False),
        ("resolved", False, False, False),
        ("new", False, False, True),
    ],
)
async def test_database_rejects_inconsistent_ticket(sessions, status, assignee, taken, closed):
    from datetime import UTC, datetime

    async with sessions.begin() as session:
        user = User(telegram_id=100, name="Admin", role="admin")
        session.add(user)
        await session.flush()
        uid = user.id
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(
                Ticket(
                    author_id=uid,
                    assignee_id=uid if assignee else None,
                    taken_at=datetime.now(UTC) if taken else None,
                    closed_at=datetime.now(UTC) if closed else None,
                    status=status,
                    priority="high",
                    description="Issue",
                    request_id="1",
                )
            )
