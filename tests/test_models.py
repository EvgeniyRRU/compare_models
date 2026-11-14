"""Tests that the models' CHECK constraints reject invalid rows in PostgreSQL."""

from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, TicketEvent, User


async def create_user(session: AsyncSession, telegram_id: int, *, role: str | None = None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user


def make_ticket(author: User, **overrides: Any) -> Ticket:
    """Build a valid ``new`` ticket, allowing individual fields to be overridden."""
    fields: dict[str, Any] = {
        "author_id": author.id,
        "description": "a valid description",
        "priority": "medium",
        "request_id": "REQ-1",
        "status": "new",
    }
    fields.update(overrides)
    return Ticket(**fields)


async def expect_integrity_error(session: AsyncSession, *instances: Any) -> None:
    session.add_all(instances)
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_valid_new_ticket_is_accepted(session: AsyncSession) -> None:
    author = await create_user(session, telegram_id=1001)
    ticket = make_ticket(author)
    session.add(ticket)
    await session.flush()
    assert ticket.id is not None


async def test_new_ticket_with_assignee_rejected(session: AsyncSession) -> None:
    author = await create_user(session, telegram_id=1001)
    assignee = await create_user(session, telegram_id=1002, role="developer")
    await expect_integrity_error(session, make_ticket(author, assignee_id=assignee.id))


async def test_in_progress_without_assignee_rejected(session: AsyncSession) -> None:
    author = await create_user(session, telegram_id=1001)
    await expect_integrity_error(
        session, make_ticket(author, status="in_progress", assignee_id=None, taken_at=None)
    )


async def test_resolved_without_closed_at_rejected(session: AsyncSession) -> None:
    author = await create_user(session, telegram_id=1001)
    await expect_integrity_error(session, make_ticket(author, status="resolved", closed_at=None))


async def test_empty_description_rejected(session: AsyncSession) -> None:
    author = await create_user(session, telegram_id=1001)
    await expect_integrity_error(session, make_ticket(author, description=""))


async def test_overlong_description_rejected(session: AsyncSession) -> None:
    author = await create_user(session, telegram_id=1001)
    await expect_integrity_error(session, make_ticket(author, description="x" * 3001))


async def test_empty_request_id_rejected(session: AsyncSession) -> None:
    author = await create_user(session, telegram_id=1001)
    await expect_integrity_error(session, make_ticket(author, request_id=""))


async def test_overlong_request_id_rejected(session: AsyncSession) -> None:
    author = await create_user(session, telegram_id=1001)
    # ``request_id`` is ``String(64)`` → ``varchar(64)``, so an over-long value
    # is rejected by the type itself (DBAPIError) before the CHECK is evaluated.
    session.add(make_ticket(author, request_id="x" * 65))
    with pytest.raises(DBAPIError):
        await session.flush()
    await session.rollback()


async def test_invalid_role_rejected(session: AsyncSession) -> None:
    await expect_integrity_error(session, User(telegram_id=1001, role="bogus"))


async def test_invalid_event_type_rejected(session: AsyncSession) -> None:
    actor = await create_user(session, telegram_id=1001, role="developer")
    ticket = make_ticket(actor)
    session.add(ticket)
    await session.flush()
    await expect_integrity_error(
        session,
        TicketEvent(ticket_id=ticket.id, actor_id=actor.id, event_type="bogus", version=1),
    )
