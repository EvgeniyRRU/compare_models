from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 1, 1, tzinfo=UTC)


async def _user(session: AsyncSession, telegram_id: int = 1) -> int:
    result = await session.execute(
        text(
            "INSERT INTO users (telegram_id, full_name, role) "
            "VALUES (:tg, 'Test', 'developer') "
            "ON CONFLICT (telegram_id) DO UPDATE SET full_name = EXCLUDED.full_name "
            "RETURNING id"
        ),
        {"tg": telegram_id},
    )
    await session.commit()
    return int(result.scalar_one())


async def _insert_ticket(session: AsyncSession, **fields: object) -> None:
    author = await _user(session, telegram_id=900)
    values: dict[str, object] = {
        "author_id": author,
        "assignee_id": None,
        "description": "d",
        "priority": "high",
        "request_id": "r1",
        "tenant_id": None,
        "status": "new",
        "version": 1,
        "taken_at": None,
        "closed_at": None,
        "next_reminder_at": None,
    }
    values.update(fields)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{key}" for key in values)
    await session.execute(text(f"INSERT INTO tickets ({columns}) VALUES ({placeholders})"), values)
    await session.commit()


async def test_new_ticket_cannot_have_assignee(session: AsyncSession) -> None:
    assignee = await _user(session)
    with pytest.raises(IntegrityError):
        await _insert_ticket(session, status="new", assignee_id=assignee)
    await session.rollback()


async def test_in_progress_requires_assignee_and_taken_at(session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        await _insert_ticket(session, status="in_progress")
    await session.rollback()


async def test_closed_at_is_set_exactly_for_final_statuses(session: AsyncSession) -> None:
    assignee = await _user(session)
    with pytest.raises(IntegrityError):
        await _insert_ticket(
            session,
            status="in_progress",
            assignee_id=assignee,
            taken_at=NOW,
            closed_at=NOW,
        )
    await session.rollback()


async def test_next_reminder_is_only_allowed_for_new_status(session: AsyncSession) -> None:
    assignee = await _user(session)
    with pytest.raises(IntegrityError):
        await _insert_ticket(
            session,
            status="in_progress",
            assignee_id=assignee,
            taken_at=NOW,
            next_reminder_at=NOW,
        )
    await session.rollback()


async def test_ticket_numbers_are_generated_and_not_reused(session: AsyncSession) -> None:
    await _insert_ticket(session)
    first = await session.scalar(text("SELECT max(id) FROM tickets"))
    await session.execute(text("DELETE FROM tickets"))
    await _insert_ticket(session)
    second = await session.scalar(text("SELECT max(id) FROM tickets"))
    assert second > first
