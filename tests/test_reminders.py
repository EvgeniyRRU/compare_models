"""Tests for the reminder scheduler (real PostgreSQL via the conftest fixtures)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.background import run_reminder_pass
from app.config import settings
from app.db.enums import Priority, Role, TicketStatus
from app.db.models import Ticket, User
from app.services.reminders import claim_due_reminders, reminder_text
from app.services.render import PRIORITY_LABELS
from app.services.ticket_service import abandon, create_ticket, take

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def interval(priority: Priority) -> timedelta:
    return timedelta(seconds=settings.reminder_interval_seconds(priority))


async def mk_user(session: AsyncSession, telegram_id: int, role: Role | None = None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user


async def seed_ticket(
    session: AsyncSession,
    author: User,
    *,
    status: TicketStatus = TicketStatus.new,
    priority: Priority = Priority.medium,
    next_reminder_at: datetime | None,
    message_id: int | None = None,
    assignee: User | None = None,
    taken_at: datetime | None = None,
) -> Ticket:
    """Insert a Ticket row directly with explicit scheduling fields."""
    ticket = Ticket(
        author_id=author.id,
        assignee_id=assignee.id if assignee is not None else None,
        description="a test ticket",
        priority=priority,
        request_id="REQ-1",
        status=status,
        version=1,
        next_reminder_at=next_reminder_at,
        message_id=message_id,
        taken_at=taken_at,
    )
    session.add(ticket)
    await session.flush()
    return ticket


async def load_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    stmt = select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalars().one()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the conftest test engine (``NullPool``)."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# 1. Schedule on new-entry & clear on exit — reminder scheduling made explicit.
async def test_reminder_schedule_set_and_cleared_across_transitions(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()

    ticket = await create_ticket(
        session,
        author=support,
        priority=Priority.critical,
        description="help",
        request_id="REQ-1",
    )
    await session.commit()
    assert ticket.next_reminder_at is not None

    ticket = await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()
    assert ticket.next_reminder_at is None

    ticket = await abandon(session, ticket_id=ticket.id, actor=developer, expected_version=2)
    await session.commit()
    assert ticket.status is TicketStatus.new
    assert ticket.next_reminder_at is not None


# 2. claim_due_reminders selects only due ``new`` tickets.
async def test_claim_due_reminders_selects_only_due_new_tickets(session: AsyncSession) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()

    now = BASE
    due = await seed_ticket(
        session, author, priority=Priority.high, next_reminder_at=now - timedelta(seconds=1)
    )
    future = await seed_ticket(
        session, author, priority=Priority.high, next_reminder_at=now + timedelta(seconds=60)
    )
    in_progress = await seed_ticket(
        session,
        author,
        status=TicketStatus.in_progress,
        next_reminder_at=now - timedelta(seconds=1),
        assignee=author,
        taken_at=now - timedelta(seconds=2),
    )
    await session.commit()

    claimed = await claim_due_reminders(session, now=now)
    await session.commit()

    assert [t.id for t in claimed] == [due.id]
    # The claimed ticket advanced to now + interval(priority).
    assert claimed[0].next_reminder_at == now + interval(Priority.high)
    # The non-claimed rows are untouched.
    assert (await load_ticket(session, future.id)).next_reminder_at == now + timedelta(seconds=60)
    assert (await load_ticket(session, in_progress.id)).next_reminder_at == now - timedelta(
        seconds=1
    )


# 3. No catch-up series after idle.
async def test_no_catch_up_series_after_idle(session: AsyncSession) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()

    now = BASE
    ticket = await seed_ticket(
        session,
        author,
        priority=Priority.low,
        next_reminder_at=now - timedelta(days=30),
    )
    await session.commit()

    first = await claim_due_reminders(session, now=now)
    await session.commit()
    assert [t.id for t in first] == [ticket.id]
    # Advanced to now + interval, NOT a catch-up chain based on the overdue time.
    assert first[0].next_reminder_at == now + interval(Priority.low)

    second = await claim_due_reminders(session, now=now)
    await session.commit()
    assert second == []


# 4. No duplicate sends under concurrent workers (SKIP LOCKED).
async def test_concurrent_workers_claim_disjoint_tickets(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()

    now = BASE
    seeded = [
        await seed_ticket(session, author, next_reminder_at=now - timedelta(seconds=1))
        for _ in range(3)
    ]
    await session.commit()

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def worker() -> list[Ticket]:
        async with factory() as s:
            claimed = await claim_due_reminders(s, now=now)
            await s.commit()
            return claimed

    # Two workers race with the same ``now``. SKIP LOCKED guarantees each due
    # row is claimed by exactly one worker (no double-send). ``asyncio.timeout``
    # fails fast if a regression blocks instead of skipping locked rows.
    async with asyncio.timeout(10):
        first, second = await asyncio.gather(worker(), worker())

    first_ids = {t.id for t in first}
    second_ids = {t.id for t in second}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {t.id for t in seeded}


# 5. run_reminder_pass sends + returns count.
async def test_run_reminder_pass_sends_and_returns_count(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()

    now = BASE
    ticket = await seed_ticket(
        session,
        author,
        next_reminder_at=now - timedelta(seconds=1),
        message_id=777,
    )
    await session.commit()

    sent: list[tuple[str, int | None]] = []

    async def send_reminder(text: str, reply_to_message_id: int | None) -> None:
        sent.append((text, reply_to_message_id))

    count = await run_reminder_pass(
        session_factory=session_factory, send_reminder=send_reminder, now=now
    )

    assert count == 1
    assert len(sent) == 1
    text, reply_to = sent[0]
    assert reply_to == ticket.message_id
    assert f"#{ticket.id}" in text
    # The claim advanced next_reminder_at, so a second pass at the same ``now``
    # is idle (no retry).
    assert (
        await run_reminder_pass(
            session_factory=session_factory, send_reminder=send_reminder, now=now
        )
        == 0
    )
    assert len(sent) == 1


# 6. run_reminder_pass catches a send error and does not retry.
async def test_run_reminder_pass_catches_send_error(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()

    now = BASE
    ok = await seed_ticket(session, author, next_reminder_at=now - timedelta(seconds=1))
    bad = await seed_ticket(session, author, next_reminder_at=now - timedelta(seconds=1))
    await session.commit()

    calls: list[int] = []

    async def send_reminder(text: str, reply_to_message_id: int | None) -> None:
        ticket_id = int(text.split("#")[1].split(" ")[0])
        calls.append(ticket_id)
        if ticket_id == bad.id:
            raise RuntimeError("send failed")

    count = await run_reminder_pass(
        session_factory=session_factory, send_reminder=send_reminder, now=now
    )

    # The failing send is caught and not retried; the successful one is counted.
    assert count == 1
    assert set(calls) == {ok.id, bad.id}


# 7. reminder_text contains the id and the Russian priority label.
def test_reminder_text_contains_id_and_priority_label() -> None:
    ticket = Ticket(
        id=12,
        author_id=1,
        description="d",
        priority=Priority.critical,
        request_id="REQ-1",
        status=TicketStatus.new,
        version=1,
    )
    text = reminder_text(ticket)

    assert "#12" in text
    assert PRIORITY_LABELS[Priority.critical] in text
    assert len(text) < 100
