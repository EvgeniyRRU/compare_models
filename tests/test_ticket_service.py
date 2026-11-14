"""Tests for the ticket service (real PostgreSQL via the conftest fixtures)."""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import settings
from app.db.enums import Priority, Role, TicketEventType, TicketStatus
from app.db.models import Ticket, TicketEvent, User
from app.services.ticket_service import (
    PermissionDenied,
    TicketAlreadyTaken,
    TicketChanged,
    _lock_user_for_share,
    abandon,
    create_ticket,
    mark_duplicate,
    reassign,
    reject,
    reopen,
    resolve,
    set_in_progress,
    set_waiting_info,
    take,
)


async def mk_user(session: AsyncSession, telegram_id: int, role: Role | None = None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user


async def fresh_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    """Load a ticket with fresh values, bypassing the identity map."""
    stmt = select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalars().one()


async def event_count(session: AsyncSession, ticket_id: int) -> int:
    return cast(
        int,
        await session.scalar(
            select(func.count()).select_from(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
        ),
    )


async def latest_event(session: AsyncSession, ticket_id: int) -> TicketEvent:
    """Return the most recently written event for a ticket."""
    stmt = (
        select(TicketEvent)
        .where(TicketEvent.ticket_id == ticket_id)
        .order_by(TicketEvent.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().one()


async def new_ticket(
    session: AsyncSession,
    author: User,
    *,
    priority: Priority = Priority.medium,
) -> Ticket:
    ticket = await create_ticket(
        session,
        author=author,
        priority=priority,
        description="a test ticket",
        request_id="REQ-1",
    )
    await session.commit()
    return ticket


async def check_invariants(session: AsyncSession, ticket_id: int) -> None:
    t = await fresh_ticket(session, ticket_id)
    final = t.status in (TicketStatus.resolved, TicketStatus.rejected, TicketStatus.duplicate)
    assert (t.closed_at is not None) is final
    if t.status is TicketStatus.new:
        assert t.assignee_id is None
        assert t.taken_at is None
    if t.status in (TicketStatus.in_progress, TicketStatus.waiting_info):
        assert t.assignee_id is not None
        assert t.taken_at is not None


async def test_create_ticket_initial_fields(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    await session.commit()

    before = datetime.now(UTC)
    ticket = await create_ticket(
        session,
        author=support,
        priority=Priority.medium,
        description="hello",
        request_id="REQ-1",
        tenant_id="tenant-1",
    )
    await session.commit()
    after = datetime.now(UTC)

    assert ticket.status is TicketStatus.new
    assert ticket.version == 1
    assert ticket.assignee_id is None
    assert ticket.taken_at is None
    assert ticket.closed_at is None
    assert ticket.tenant_id == "tenant-1"
    interval = settings.reminder_interval_seconds(Priority.medium)
    assert ticket.next_reminder_at is not None
    assert (
        before + timedelta(seconds=interval)
        <= ticket.next_reminder_at
        <= after + timedelta(seconds=interval)
    )
    assert await event_count(session, ticket.id) == 1


async def test_support_can_create_developer_cannot(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()

    ticket = await create_ticket(
        session, author=support, priority=Priority.low, description="d", request_id="REQ-1"
    )
    await session.commit()
    assert ticket.status is TicketStatus.new

    with pytest.raises(PermissionDenied):
        await create_ticket(
            session, author=developer, priority=Priority.low, description="d", request_id="REQ-2"
        )
    assert await event_count(session, ticket.id) == 1


async def test_full_cycle_bumps_version_and_writes_events(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()

    ticket = await new_ticket(session, support)
    assert ticket.version == 1
    assert await event_count(session, ticket.id) == 1

    ticket = await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()
    assert ticket.version == 2
    assert ticket.status is TicketStatus.in_progress
    assert ticket.assignee_id == developer.id
    assert await event_count(session, ticket.id) == 2

    event = await latest_event(session, ticket.id)
    assert event.event_type is TicketEventType.taken
    assert event.old_status == "new"
    assert event.new_status == "in_progress"
    assert event.old_assignee_id is None
    assert event.new_assignee_id == developer.id
    assert event.version == 2

    ticket = await set_waiting_info(
        session, ticket_id=ticket.id, actor=developer, expected_version=2
    )
    await session.commit()
    assert ticket.version == 3
    assert ticket.status is TicketStatus.waiting_info
    assert await event_count(session, ticket.id) == 3

    event = await latest_event(session, ticket.id)
    assert event.event_type is TicketEventType.status_changed
    assert event.old_status == "in_progress"
    assert event.new_status == "waiting_info"
    assert event.old_assignee_id == developer.id
    assert event.new_assignee_id == developer.id
    assert event.version == 3

    ticket = await set_in_progress(
        session, ticket_id=ticket.id, actor=developer, expected_version=3
    )
    await session.commit()
    assert ticket.version == 4
    assert ticket.status is TicketStatus.in_progress
    assert await event_count(session, ticket.id) == 4

    event = await latest_event(session, ticket.id)
    assert event.event_type is TicketEventType.status_changed
    assert event.old_status == "waiting_info"
    assert event.new_status == "in_progress"
    assert event.old_assignee_id == developer.id
    assert event.new_assignee_id == developer.id
    assert event.version == 4


async def test_one_transition_one_bump_one_event(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)

    events_before = await event_count(session, ticket.id)
    ticket = await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()

    assert ticket.version == 2
    assert await event_count(session, ticket.id) == events_before + 1


async def test_stale_version_raises_ticket_changed_and_no_change(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)

    await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()

    with pytest.raises(TicketChanged):
        await set_waiting_info(session, ticket_id=ticket.id, actor=developer, expected_version=1)

    t = await fresh_ticket(session, ticket.id)
    assert t.status is TicketStatus.in_progress
    assert t.version == 2
    assert await event_count(session, ticket.id) == 2


async def test_take_already_taken_raises_ticket_already_taken(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)

    await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()

    with pytest.raises(TicketAlreadyTaken):
        await take(session, ticket_id=ticket.id, actor=developer2, expected_version=1)

    t = await fresh_ticket(session, ticket.id)
    assert t.assignee_id == developer1.id


async def test_take_race_atomicity(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)

    first = await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()
    assert first.assignee_id == developer1.id

    with pytest.raises(TicketAlreadyTaken):
        await take(session, ticket_id=ticket.id, actor=developer2, expected_version=1)

    t = await fresh_ticket(session, ticket.id)
    assert t.assignee_id == developer1.id
    assert t.version == 2


async def test_take_race_atomicity_two_sessions(session: AsyncSession, engine: AsyncEngine) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    ticket_id = ticket.id

    # Two independent sessions race for the same ``new`` ticket. Under a true
    # concurrent interleaving the loser may see ``TicketChanged`` instead of
    # ``TicketAlreadyTaken``, depending on whether the winner commits before the
    # loser's 0-row re-read. Callers should treat both as "refresh": neither
    # mutated the ticket.
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s1, factory() as s2:
        first = await take(s1, ticket_id=ticket_id, actor=developer1, expected_version=1)
        await s1.commit()
        assert first.assignee_id == developer1.id

        with pytest.raises(TicketAlreadyTaken):
            await take(s2, ticket_id=ticket_id, actor=developer2, expected_version=1)

    t = await fresh_ticket(session, ticket_id)
    assert t.assignee_id == developer1.id


async def test_take_clears_next_reminder_at(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    assert ticket.next_reminder_at is not None

    ticket = await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()
    assert ticket.next_reminder_at is None


async def test_abandon_resets_assignee_and_sets_reminder(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    ticket = await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()

    ticket = await abandon(session, ticket_id=ticket.id, actor=developer, expected_version=2)
    await session.commit()

    assert ticket.status is TicketStatus.new
    assert ticket.assignee_id is None
    assert ticket.taken_at is None
    assert ticket.closed_at is None
    assert ticket.next_reminder_at is not None
    assert ticket.version == 3
    await check_invariants(session, ticket.id)


async def test_resolve_sets_closed_at(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    ticket = await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()

    ticket = await resolve(session, ticket_id=ticket.id, actor=developer, expected_version=2)
    await session.commit()

    assert ticket.status is TicketStatus.resolved
    assert ticket.closed_at is not None
    await check_invariants(session, ticket.id)


async def test_reopen_resets_and_sets_reminder(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    admin = await mk_user(session, 1003, Role.admin)
    await session.commit()
    ticket = await new_ticket(session, support)
    ticket = await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()
    ticket = await resolve(session, ticket_id=ticket.id, actor=developer, expected_version=2)
    await session.commit()
    assert ticket.closed_at is not None

    ticket = await reopen(session, ticket_id=ticket.id, actor=admin, expected_version=3)
    await session.commit()

    assert ticket.status is TicketStatus.new
    assert ticket.assignee_id is None
    assert ticket.taken_at is None
    assert ticket.closed_at is None
    assert ticket.next_reminder_at is not None
    await check_invariants(session, ticket.id)


async def test_closed_at_invariant_across_transitions(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    admin = await mk_user(session, 1003, Role.admin)
    await session.commit()
    ticket = await new_ticket(session, support)
    await check_invariants(session, ticket.id)

    ticket = await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()
    await check_invariants(session, ticket.id)

    ticket = await set_waiting_info(
        session, ticket_id=ticket.id, actor=developer, expected_version=2
    )
    await session.commit()
    await check_invariants(session, ticket.id)

    ticket = await resolve(session, ticket_id=ticket.id, actor=developer, expected_version=3)
    await session.commit()
    await check_invariants(session, ticket.id)

    ticket = await reopen(session, ticket_id=ticket.id, actor=admin, expected_version=4)
    await session.commit()
    await check_invariants(session, ticket.id)


async def test_non_assignee_cannot_resolve(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()

    with pytest.raises(PermissionDenied):
        await resolve(session, ticket_id=ticket.id, actor=developer2, expected_version=2)


async def test_non_admin_cannot_reopen(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()
    await resolve(session, ticket_id=ticket.id, actor=developer, expected_version=2)
    await session.commit()

    with pytest.raises(PermissionDenied):
        await reopen(session, ticket_id=ticket.id, actor=developer, expected_version=3)


async def test_non_admin_cannot_reassign(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()

    with pytest.raises(PermissionDenied):
        await reassign(
            session,
            ticket_id=ticket.id,
            actor=developer1,
            new_assignee=developer2,
            expected_version=2,
        )


async def test_reassign_to_non_developer_denied(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    admin = await mk_user(session, 1003, Role.admin)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()

    with pytest.raises(PermissionDenied):
        await reassign(
            session, ticket_id=ticket.id, actor=admin, new_assignee=support, expected_version=2
        )


async def test_reassign_changes_assignee_and_bumps_version(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    admin = await mk_user(session, 1004, Role.admin)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()

    ticket = await reassign(
        session, ticket_id=ticket.id, actor=admin, new_assignee=developer2, expected_version=2
    )
    await session.commit()

    assert ticket.status is TicketStatus.in_progress
    assert ticket.assignee_id == developer2.id
    assert ticket.taken_at is not None
    assert ticket.version == 3
    assert await event_count(session, ticket.id) == 3

    event = await latest_event(session, ticket.id)
    assert event.event_type is TicketEventType.reassigned
    assert event.old_status == "in_progress"
    assert event.new_status == "in_progress"
    assert event.old_assignee_id == developer1.id
    assert event.new_assignee_id == developer2.id
    assert event.version == 3


async def test_reject_and_duplicate_finalize(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer = await mk_user(session, 1002, Role.developer)
    admin = await mk_user(session, 1003, Role.admin)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()

    ticket = await reject(session, ticket_id=ticket.id, actor=developer, expected_version=2)
    await session.commit()
    assert ticket.status is TicketStatus.rejected
    assert ticket.closed_at is not None

    await reopen(session, ticket_id=ticket.id, actor=admin, expected_version=3)
    await session.commit()
    ticket = await fresh_ticket(session, ticket.id)
    assert ticket.status is TicketStatus.new

    await take(session, ticket_id=ticket.id, actor=developer, expected_version=4)
    await session.commit()
    ticket = await mark_duplicate(session, ticket_id=ticket.id, actor=developer, expected_version=5)
    await session.commit()
    assert ticket.status is TicketStatus.duplicate
    assert ticket.closed_at is not None


async def test_denied_resolve_leaves_no_side_effects(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()

    before = await fresh_ticket(session, ticket.id)
    events_before = await event_count(session, ticket.id)

    with pytest.raises(PermissionDenied):
        await resolve(session, ticket_id=ticket.id, actor=developer2, expected_version=2)

    after = await fresh_ticket(session, ticket.id)
    assert after.version == before.version
    assert after.status is before.status
    assert after.assignee_id == before.assignee_id
    assert after.closed_at == before.closed_at
    assert await event_count(session, ticket.id) == events_before


async def test_non_assignee_cannot_set_waiting_info(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()

    with pytest.raises(PermissionDenied):
        await set_waiting_info(session, ticket_id=ticket.id, actor=developer2, expected_version=2)


async def test_non_assignee_cannot_set_in_progress(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()
    await set_waiting_info(session, ticket_id=ticket.id, actor=developer1, expected_version=2)
    await session.commit()

    with pytest.raises(PermissionDenied):
        await set_in_progress(session, ticket_id=ticket.id, actor=developer2, expected_version=3)


async def test_non_assignee_cannot_abandon(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()

    with pytest.raises(PermissionDenied):
        await abandon(session, ticket_id=ticket.id, actor=developer2, expected_version=2)


async def test_roleless_cannot_create(session: AsyncSession) -> None:
    roleless = await mk_user(session, 1001)
    await session.commit()

    with pytest.raises(PermissionDenied):
        await create_ticket(
            session, author=roleless, priority=Priority.low, description="d", request_id="REQ-1"
        )

    count = cast(int, await session.scalar(select(func.count()).select_from(Ticket)))
    assert count == 0


async def test_lock_user_for_share_returns_role(session: AsyncSession) -> None:
    developer = await mk_user(session, 1002, Role.developer)
    await session.commit()

    role = await _lock_user_for_share(session, developer.id)

    assert role is Role.developer


async def test_reassign_stale_version_raises_ticket_changed(session: AsyncSession) -> None:
    support = await mk_user(session, 1001, Role.support)
    developer1 = await mk_user(session, 1002, Role.developer)
    developer2 = await mk_user(session, 1003, Role.developer)
    admin = await mk_user(session, 1004, Role.admin)
    await session.commit()
    ticket = await new_ticket(session, support)
    await take(session, ticket_id=ticket.id, actor=developer1, expected_version=1)
    await session.commit()

    with pytest.raises(TicketChanged):
        await reassign(
            session, ticket_id=ticket.id, actor=admin, new_assignee=developer2, expected_version=1
        )

    t = await fresh_ticket(session, ticket.id)
    assert t.assignee_id == developer1.id
    assert t.version == 2
