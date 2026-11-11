import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.db.models import Ticket, TicketEvent
from support_bot.domain.errors import (
    AlreadyTaken,
    Forbidden,
    InvalidTransition,
    StaleTicket,
    ValidationFailed,
)
from support_bot.domain.state_machine import Action, Status
from support_bot.services import tickets as svc
from tests.helpers import NOW, add_user


async def _create(session: AsyncSession, settings: Settings, author: int) -> int:
    ticket = await svc.create_ticket(
        session,
        settings=settings,
        actor_id=author,
        description="Не открывается отчёт",
        priority="high",
        request_id="REQ-1",
        tenant_id=None,
        now=NOW,
    )
    await session.commit()
    return ticket.id


async def _act(
    session: AsyncSession,
    settings: Settings,
    *,
    ticket_id: int,
    actor: int,
    action: Action,
    version: int,
    status: Status,
    target: int | None = None,
    now_offset: int = 0,
) -> Ticket:
    ticket = await svc.transition(
        session,
        settings=settings,
        actor_id=actor,
        ticket_id=ticket_id,
        action=action,
        expected_version=version,
        expected_status=status,
        target_user_id=target,
        now=NOW + timedelta(minutes=now_offset),
    )
    await session.commit()
    return ticket


async def test_support_creates_ticket_with_reminder_from_priority(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 1, "support")

    ticket_id = await _create(session, settings, support)

    ticket = await session.get(Ticket, ticket_id)
    assert ticket is not None
    assert ticket.status == "new"
    assert ticket.version == 1
    assert ticket.post_synced_version == 0
    assert ticket.assignee_id is None
    assert ticket.next_reminder_at == NOW + timedelta(minutes=settings.reminder_high_minutes)
    events = (await session.scalars(select(TicketEvent))).all()
    assert [e.event_type for e in events] == ["created"]


async def test_developer_cannot_create_ticket(session: AsyncSession, settings: Settings) -> None:
    developer = await add_user(session, 2, "developer")

    with pytest.raises(Forbidden):
        await _create(session, settings, developer)


async def test_empty_description_is_rejected(session: AsyncSession, settings: Settings) -> None:
    support = await add_user(session, 3, "support")

    with pytest.raises(ValidationFailed):
        await svc.create_ticket(
            session,
            settings=settings,
            actor_id=support,
            description="   ",
            priority="low",
            request_id="R",
            tenant_id=None,
            now=NOW,
        )


async def test_take_sets_assignee_and_bumps_version_once(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 10, "support")
    developer = await add_user(session, 11, "developer")
    ticket_id = await _create(session, settings, support)

    ticket = await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=developer,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )

    assert ticket.status == "in_progress"
    assert ticket.assignee_id == developer
    assert ticket.taken_at == NOW
    assert ticket.next_reminder_at is None
    assert ticket.version == 2


async def test_second_taker_gets_already_taken(session: AsyncSession, settings: Settings) -> None:
    support = await add_user(session, 20, "support")
    first = await add_user(session, 21, "developer")
    second = await add_user(session, 22, "developer")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=first,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )

    with pytest.raises(AlreadyTaken):
        await _act(
            session,
            settings,
            ticket_id=ticket_id,
            actor=second,
            action=Action.TAKE,
            version=1,
            status=Status.NEW,
        )


async def test_concurrent_takes_let_exactly_one_win(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    support = await add_user(session, 30, "support")
    devs = [await add_user(session, 31 + i, "developer") for i in range(5)]
    ticket_id = await _create(session, settings, support)

    async def take(dev: int) -> str:
        async with session_factory() as s:
            try:
                await _act(
                    s,
                    settings,
                    ticket_id=ticket_id,
                    actor=dev,
                    action=Action.TAKE,
                    version=1,
                    status=Status.NEW,
                )
                return "ok"
            except AlreadyTaken:
                return "taken"

    results = await asyncio.gather(*(take(d) for d in devs))

    assert results.count("ok") == 1
    assert results.count("taken") == 4


async def test_stale_button_after_in_progress_waiting_info_cycle_is_rejected(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 40, "support")
    dev = await add_user(session, 41, "developer")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )
    # the "В ожидании информации" button rendered at version 2
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.WAIT_INFO,
        version=2,
        status=Status.IN_PROGRESS,
    )
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.RESUME,
        version=3,
        status=Status.WAITING_INFO,
    )

    # an old "В ожидании" button from version 2 must not apply in in_progress at version 4
    with pytest.raises(StaleTicket):
        await _act(
            session,
            settings,
            ticket_id=ticket_id,
            actor=dev,
            action=Action.WAIT_INFO,
            version=2,
            status=Status.IN_PROGRESS,
        )


async def test_every_business_change_writes_one_event_with_new_version(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 50, "support")
    dev = await add_user(session, 51, "developer")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.RESOLVE,
        version=2,
        status=Status.IN_PROGRESS,
    )

    events = (
        await session.scalars(
            select(TicketEvent).where(TicketEvent.ticket_id == ticket_id).order_by(TicketEvent.id)
        )
    ).all()
    assert [(e.event_type, e.version) for e in events] == [
        ("created", 1),
        ("taken", 2),
        ("status_changed", 3),
    ]
    assert events[1].from_status == "new" and events[1].to_status == "in_progress"
    assert events[1].to_assignee_id == dev


async def test_resolve_sets_closed_at_and_clears_reminder(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 60, "support")
    dev = await add_user(session, 61, "developer")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )

    ticket = await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.RESOLVE,
        version=2,
        status=Status.IN_PROGRESS,
        now_offset=5,
    )

    assert ticket.status == "resolved"
    assert ticket.closed_at == NOW + timedelta(minutes=5)
    assert ticket.next_reminder_at is None


async def test_new_ticket_cannot_be_closed_directly(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 70, "support")
    admin = await add_user(session, 71, "admin")
    ticket_id = await _create(session, settings, support)

    with pytest.raises(InvalidTransition):
        await _act(
            session,
            settings,
            ticket_id=ticket_id,
            actor=admin,
            action=Action.REJECT,
            version=1,
            status=Status.NEW,
        )


async def test_release_returns_ticket_to_queue_and_schedules_reminder(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 80, "support")
    dev = await add_user(session, 81, "developer")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )

    ticket = await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.RELEASE,
        version=2,
        status=Status.IN_PROGRESS,
        now_offset=3,
    )

    assert ticket.status == "new"
    assert ticket.assignee_id is None
    assert ticket.taken_at is None
    assert ticket.next_reminder_at == NOW + timedelta(minutes=3 + settings.reminder_high_minutes)
    assert ticket.version == 3


async def test_reopen_is_admin_only_and_clears_closing_fields(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 90, "support")
    dev = await add_user(session, 91, "developer")
    admin = await add_user(session, 92, "admin")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.REJECT,
        version=2,
        status=Status.IN_PROGRESS,
    )

    with pytest.raises(Forbidden):
        await _act(
            session,
            settings,
            ticket_id=ticket_id,
            actor=dev,
            action=Action.REOPEN,
            version=3,
            status=Status.REJECTED,
        )

    ticket = await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=admin,
        action=Action.REOPEN,
        version=3,
        status=Status.REJECTED,
    )
    assert ticket.status == "new"
    assert ticket.assignee_id is None
    assert ticket.taken_at is None
    assert ticket.closed_at is None
    assert ticket.next_reminder_at is not None


async def test_reassign_by_admin_keeps_status_and_resets_taken_at(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 100, "support")
    dev = await add_user(session, 101, "developer")
    new_dev = await add_user(session, 102, "developer")
    admin = await add_user(session, 103, "admin")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )

    ticket = await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=admin,
        action=Action.REASSIGN,
        version=2,
        status=Status.IN_PROGRESS,
        target=new_dev,
        now_offset=7,
    )

    assert ticket.status == "in_progress"
    assert ticket.assignee_id == new_dev
    assert ticket.taken_at == NOW + timedelta(minutes=7)
    assert ticket.version == 3


async def test_reassign_target_must_be_developer_or_admin(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 110, "support")
    dev = await add_user(session, 111, "developer")
    plain_support = await add_user(session, 112, "support")
    admin = await add_user(session, 113, "admin")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )

    with pytest.raises(ValidationFailed):
        await _act(
            session,
            settings,
            ticket_id=ticket_id,
            actor=admin,
            action=Action.REASSIGN,
            version=2,
            status=Status.IN_PROGRESS,
            target=plain_support,
        )


async def test_closed_at_column_matches_status_after_reopen_cycle(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 120, "support")
    dev = await add_user(session, 121, "developer")
    admin = await add_user(session, 122, "admin")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.DUPLICATE,
        version=2,
        status=Status.IN_PROGRESS,
    )
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=admin,
        action=Action.REOPEN,
        version=3,
        status=Status.DUPLICATE,
    )

    row = await session.scalar(
        text("SELECT closed_at FROM tickets WHERE id = :id"), {"id": ticket_id}
    )
    assert row is None


async def test_button_carries_only_version_and_status_is_resolved_from_db(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 130, "support")
    dev = await add_user(session, 131, "developer")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )

    ticket = await svc.transition_at_version(
        session,
        settings=settings,
        actor_id=dev,
        ticket_id=ticket_id,
        action=Action.WAIT_INFO,
        expected_version=2,
        now=NOW,
    )
    await session.commit()

    assert ticket.status == "waiting_info"
    assert ticket.version == 3


async def test_button_at_old_version_is_stale(session: AsyncSession, settings: Settings) -> None:
    support = await add_user(session, 140, "support")
    dev = await add_user(session, 141, "developer")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
    )

    with pytest.raises(StaleTicket):
        await svc.transition_at_version(
            session,
            settings=settings,
            actor_id=dev,
            ticket_id=ticket_id,
            action=Action.WAIT_INFO,
            expected_version=1,
            now=NOW,
        )


async def test_time_to_take_counts_from_last_entry_into_new(
    session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 150, "support")
    dev = await add_user(session, 151, "developer")
    ticket_id = await _create(session, settings, support)
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=1,
        status=Status.NEW,
        now_offset=1,
    )
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.RELEASE,
        version=2,
        status=Status.IN_PROGRESS,
        now_offset=10,
    )
    await _act(
        session,
        settings,
        ticket_id=ticket_id,
        actor=dev,
        action=Action.TAKE,
        version=3,
        status=Status.NEW,
        now_offset=25,
    )

    seconds = await svc.time_to_take_seconds(session, ticket_id)

    assert seconds == 15 * 60
