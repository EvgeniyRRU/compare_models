from datetime import UTC

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.db.models import Priority, Role, Ticket, TicketStatus, User


async def _make_users(db_session) -> tuple[User, User]:
    author = User(telegram_id=1, username="author", full_name="Автор", role=Role.support)
    dev = User(telegram_id=2, username="dev", full_name="Разработчик", role=Role.developer)
    db_session.add_all([author, dev])
    await db_session.flush()
    return author, dev


async def test_ticket_roundtrip(db_session) -> None:
    author, _dev = await _make_users(db_session)
    ticket = Ticket(
        author_id=author.telegram_id,
        description="Не работает выгрузка отчётов",
        priority=Priority.high,
        request_id="REQ-42",
    )
    db_session.add(ticket)
    await db_session.flush()
    assert ticket.id == 1  # identity, starts at 1
    assert ticket.version == 1
    assert ticket.status is TicketStatus.new
    assert ticket.post_synced_version == 0
    assert ticket.created_at is not None and ticket.updated_at is not None

    loaded = await db_session.get(Ticket, ticket.id)
    assert loaded is not None and loaded.request_id == "REQ-42"


async def test_new_with_assignee_violates_check(db_session) -> None:
    author, dev = await _make_users(db_session)
    from datetime import datetime

    ticket = Ticket(
        author_id=author.telegram_id,
        description="x",
        priority=Priority.low,
        request_id="REQ-1",
        status=TicketStatus.new,
        assignee_id=dev.telegram_id,
        taken_at=datetime.now(UTC),
    )
    db_session.add(ticket)
    with pytest.raises(IntegrityError, match="ck_new_no_assignee"):
        await db_session.flush()


async def test_in_progress_without_assignee_violates_check(db_session) -> None:
    author, _ = await _make_users(db_session)
    ticket = Ticket(
        author_id=author.telegram_id,
        description="x",
        priority=Priority.low,
        request_id="REQ-1",
        status=TicketStatus.in_progress,
    )
    db_session.add(ticket)
    with pytest.raises(IntegrityError, match="ck_active_has_assignee"):
        await db_session.flush()


async def test_closed_at_iff_final(db_session) -> None:
    author, dev = await _make_users(db_session)
    from datetime import datetime

    ticket = Ticket(
        author_id=author.telegram_id,
        assignee_id=dev.telegram_id,
        taken_at=datetime.now(UTC),
        description="x",
        priority=Priority.medium,
        request_id="REQ-1",
        status=TicketStatus.in_progress,
        closed_at=datetime.now(UTC),
    )
    db_session.add(ticket)
    with pytest.raises(IntegrityError, match="ck_closed_iff_final"):
        await db_session.flush()


async def test_identity_rejects_explicit_id(db_session) -> None:
    author, _ = await _make_users(db_session)
    ticket = Ticket(
        id=555,
        author_id=author.telegram_id,
        description="x",
        priority=Priority.low,
        request_id="REQ-1",
    )
    db_session.add(ticket)
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="cannot insert a non-DEFAULT value"):
        await db_session.flush()


async def test_updated_at_trigger_on_raw_update(db_session, engine) -> None:
    author, _ = await _make_users(db_session)
    ticket = Ticket(
        author_id=author.telegram_id,
        description="before",
        priority=Priority.low,
        request_id="REQ-1",
    )
    db_session.add(ticket)
    await db_session.commit()
    tid = ticket.id
    before = ticket.updated_at

    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_sleep(0.02)"))
        await conn.execute(text("UPDATE tickets SET description='after' WHERE id=:i"), {"i": tid})

    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT updated_at, created_at FROM tickets WHERE id=:i"), {"i": tid}
            )
        ).one()
    assert row.updated_at > before

    got = await db_session.get(Ticket, tid)
    assert got is not None
    assert await db_session.scalar(select(Ticket.description).where(Ticket.id == tid)) == "after"


async def test_one_draft_per_user(db_session) -> None:
    import uuid as uuidm

    from app.db.models import DraftStep, TicketDraft

    _author, _ = await _make_users(db_session)
    d1 = TicketDraft(id=uuidm.uuid4(), user_id=1, step=DraftStep.description)
    db_session.add(d1)
    await db_session.flush()
    d2 = TicketDraft(id=uuidm.uuid4(), user_id=1, step=DraftStep.priority)
    db_session.add(d2)
    with pytest.raises(IntegrityError, match="uq_ticket_drafts_user"):
        await db_session.flush()
