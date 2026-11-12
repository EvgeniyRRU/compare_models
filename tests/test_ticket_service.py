import asyncio
import time

import pytest
from sqlalchemy import delete, select

from app.db.models import Priority, Role, Ticket, TicketEvent, TicketStatus, User
from app.db.session import SessionLocal
from app.services.roles import RoleChangeError, change_user_role
from app.services.tickets import TicketError, create_ticket, transition


@pytest.mark.asyncio
async def test_admin_can_take_and_finish_ticket(db_session):
    support = User(telegram_id=101, full_name="Support", role=Role.SUPPORT)
    admin = User(telegram_id=102, full_name="Admin", role=Role.ADMIN)
    db_session.add_all([support, admin])
    await db_session.flush()
    ticket = await create_ticket(
        db_session,
        author=support,
        description="Broken",
        priority=Priority.HIGH,
        request_id="REQ-1",
        tenant_id=None,
    )
    assert ticket.next_reminder_at is not None
    ticket = await transition(
        db_session,
        actor=admin,
        ticket_id=ticket.id,
        expected_version=1,
        expected_status=TicketStatus.NEW,
        action="take",
    )
    taken_at = ticket.taken_at
    assert ticket.version == 2
    assert ticket.assignee_id == admin.id
    assert ticket.next_reminder_at is None
    ticket = await transition(
        db_session,
        actor=admin,
        ticket_id=ticket.id,
        expected_version=2,
        expected_status=TicketStatus.IN_PROGRESS,
        action="waiting",
    )
    assert ticket.version == 3
    assert ticket.taken_at == taken_at
    events = (
        await db_session.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id))
    ).all()
    assert [event.kind for event in events] == ["created", "taken", "status_changed"]


@pytest.mark.asyncio
async def test_new_ticket_cannot_be_closed_and_old_version_conflicts(db_session):
    support = User(telegram_id=201, role=Role.SUPPORT)
    dev = User(telegram_id=202, role=Role.DEVELOPER)
    db_session.add_all([support, dev])
    await db_session.flush()
    ticket = await create_ticket(
        db_session,
        author=support,
        description="Broken",
        priority=Priority.LOW,
        request_id="REQ-2",
        tenant_id=None,
    )
    with pytest.raises(TicketError):
        await transition(
            db_session,
            actor=dev,
            ticket_id=ticket.id,
            expected_version=1,
            expected_status=TicketStatus.NEW,
            action="resolve",
        )
    ticket = await transition(
        db_session,
        actor=dev,
        ticket_id=ticket.id,
        expected_version=1,
        expected_status=TicketStatus.NEW,
        action="take",
    )
    with pytest.raises(TicketError, match="изменилось"):
        await transition(
            db_session,
            actor=dev,
            ticket_id=ticket.id,
            expected_version=1,
            expected_status=TicketStatus.NEW,
            action="take",
        )


@pytest.mark.asyncio
async def test_old_button_stays_stale_after_status_cycle(db_session):
    support = User(telegram_id=301, role=Role.SUPPORT)
    dev = User(telegram_id=302, role=Role.DEVELOPER)
    db_session.add_all([support, dev])
    await db_session.flush()
    ticket = await create_ticket(
        db_session,
        author=support,
        description="Cycle",
        priority=Priority.MEDIUM,
        request_id="REQ-3",
        tenant_id=None,
    )
    ticket = await transition(
        db_session,
        actor=dev,
        ticket_id=ticket.id,
        expected_version=1,
        expected_status=TicketStatus.NEW,
        action="take",
    )
    ticket = await transition(
        db_session,
        actor=dev,
        ticket_id=ticket.id,
        expected_version=2,
        expected_status=TicketStatus.IN_PROGRESS,
        action="waiting",
    )
    ticket = await transition(
        db_session,
        actor=dev,
        ticket_id=ticket.id,
        expected_version=3,
        expected_status=TicketStatus.WAITING_INFO,
        action="resume",
    )
    with pytest.raises(TicketError, match="изменилось"):
        await transition(
            db_session,
            actor=dev,
            ticket_id=ticket.id,
            expected_version=2,
            expected_status=TicketStatus.IN_PROGRESS,
            action="resolve",
        )


@pytest.mark.asyncio
async def test_developer_role_cannot_be_removed_while_ticket_is_active(db_session):
    support = User(telegram_id=401, role=Role.SUPPORT)
    admin = User(telegram_id=402, role=Role.ADMIN)
    dev = User(telegram_id=403, role=Role.DEVELOPER)
    db_session.add_all([support, admin, dev])
    await db_session.flush()
    ticket = await create_ticket(
        db_session,
        author=support,
        description="Active",
        priority=Priority.HIGH,
        request_id="REQ-4",
        tenant_id=None,
    )
    ticket = await transition(
        db_session,
        actor=dev,
        ticket_id=ticket.id,
        expected_version=1,
        expected_status=TicketStatus.NEW,
        action="take",
    )
    with pytest.raises(RoleChangeError) as error:
        await change_user_role(db_session, actor_id=admin.id, target_id=dev.id, role=Role.SUPPORT)
    assert error.value.active_ticket_ids == [ticket.id]


@pytest.mark.asyncio
async def test_only_admin_can_reassign_and_reopen(db_session):
    support = User(telegram_id=501, role=Role.SUPPORT)
    admin = User(telegram_id=502, role=Role.ADMIN)
    dev = User(telegram_id=503, role=Role.DEVELOPER)
    new_dev = User(telegram_id=504, role=Role.DEVELOPER)
    db_session.add_all([support, admin, dev, new_dev])
    await db_session.flush()
    ticket = await create_ticket(
        db_session,
        author=support,
        description="Admin actions",
        priority=Priority.HIGH,
        request_id="REQ-5",
        tenant_id=None,
    )
    ticket = await transition(
        db_session,
        actor=dev,
        ticket_id=ticket.id,
        expected_version=1,
        expected_status=TicketStatus.NEW,
        action="take",
    )
    with pytest.raises(TicketError):
        await transition(
            db_session,
            actor=dev,
            ticket_id=ticket.id,
            expected_version=2,
            expected_status=TicketStatus.IN_PROGRESS,
            action="reassign",
            assignee_id=new_dev.id,
        )
    ticket = await transition(
        db_session,
        actor=admin,
        ticket_id=ticket.id,
        expected_version=2,
        expected_status=TicketStatus.IN_PROGRESS,
        action="reassign",
        assignee_id=new_dev.id,
    )
    assert ticket.assignee_id == new_dev.id
    assert ticket.version == 3
    ticket = await transition(
        db_session,
        actor=new_dev,
        ticket_id=ticket.id,
        expected_version=3,
        expected_status=TicketStatus.IN_PROGRESS,
        action="resolve",
    )
    assert ticket.closed_at is not None
    with pytest.raises(TicketError):
        await transition(
            db_session,
            actor=new_dev,
            ticket_id=ticket.id,
            expected_version=4,
            expected_status=TicketStatus.RESOLVED,
            action="reopen",
        )
    ticket = await transition(
        db_session,
        actor=admin,
        ticket_id=ticket.id,
        expected_version=4,
        expected_status=TicketStatus.RESOLVED,
        action="reopen",
    )
    assert ticket.status == TicketStatus.NEW
    assert ticket.assignee_id is None and ticket.taken_at is None and ticket.closed_at is None
    assert ticket.next_reminder_at is not None


@pytest.mark.asyncio
async def test_concurrent_take_has_exactly_one_winner():
    telegram_id = int(time.time() * 1_000_000)
    async with SessionLocal.begin() as session:
        support = User(telegram_id=telegram_id, role=Role.SUPPORT)
        first_dev = User(telegram_id=telegram_id + 1, role=Role.DEVELOPER)
        second_dev = User(telegram_id=telegram_id + 2, role=Role.ADMIN)
        session.add_all([support, first_dev, second_dev])
        await session.flush()
        ticket = await create_ticket(
            session,
            author=support,
            description="Concurrent take",
            priority=Priority.HIGH,
            request_id=f"RACE-{telegram_id}",
            tenant_id=None,
        )
        ticket_id = ticket.id
        actor_ids = (first_dev.id, second_dev.id)
        user_ids = (support.id, first_dev.id, second_dev.id)

    async def attempt(actor_id):
        try:
            async with SessionLocal.begin() as session:
                actor = await session.get(User, actor_id)
                await transition(
                    session,
                    actor=actor,
                    ticket_id=ticket_id,
                    expected_version=1,
                    expected_status=TicketStatus.NEW,
                    action="take",
                )
            return True
        except TicketError:
            return False

    try:
        assert sum(await asyncio.gather(*(attempt(actor_id) for actor_id in actor_ids))) == 1
        async with SessionLocal() as session:
            ticket = await session.get(Ticket, ticket_id)
            events = (
                await session.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket_id))
            ).all()
            assert ticket.version == 2
            assert ticket.status == TicketStatus.IN_PROGRESS
            assert ticket.assignee_id in actor_ids
            assert len(events) == 2
    finally:
        async with SessionLocal.begin() as session:
            await session.execute(delete(TicketEvent).where(TicketEvent.ticket_id == ticket_id))
            await session.execute(delete(Ticket).where(Ticket.id == ticket_id))
            await session.execute(delete(User).where(User.id.in_(user_ids)))
