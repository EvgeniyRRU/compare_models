from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from support_bot.db.enums import TicketEventType, TicketPriority, TicketStatus, UserRole
from support_bot.db.models.ticket import Ticket
from support_bot.db.models.ticket_event import TicketEvent
from support_bot.domain.errors import (
    AlreadyTaken,
    PermissionDenied,
    StatusConflict,
    ValidationError,
    VersionConflict,
)
from support_bot.domain.state_machine import TicketAction
from support_bot.services import tickets as service


async def test_create_ticket_starts_new_with_reminder_and_event(
    session_factory, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=1, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id, priority=TicketPriority.CRITICAL)

    assert ticket.status is TicketStatus.NEW
    assert ticket.version == 1
    assert ticket.assignee_id is None
    assert ticket.taken_at is None
    assert ticket.closed_at is None
    assert ticket.message_id is None
    assert ticket.post_synced_version == 0
    assert ticket.next_reminder_at is not None
    delta = ticket.next_reminder_at - datetime.now(UTC)
    assert timedelta(minutes=8) < delta < timedelta(minutes=12)

    async with session_factory() as session:
        events = list(
            (
                await session.scalars(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id))
            ).all()
        )
    assert len(events) == 1
    assert events[0].event_type is TicketEventType.CREATED
    assert events[0].new_status is TicketStatus.NEW
    assert events[0].version == 1


async def test_create_requires_support_or_admin(session_factory, make_user) -> None:
    developer = await make_user(telegram_id=2, role=UserRole.DEVELOPER)
    no_role = await make_user(telegram_id=3, role=None)
    async with session_factory() as session:
        with pytest.raises(PermissionDenied):
            await service.create_ticket(
                session,
                actor_id=developer.id,
                description="d",
                priority=TicketPriority.LOW,
                request_id="r",
                tenant_id=None,
                policy=_policy(),
            )
    async with session_factory() as session:
        with pytest.raises(PermissionDenied):
            await service.create_ticket(
                session,
                actor_id=no_role.id,
                description="d",
                priority=TicketPriority.LOW,
                request_id="r",
                tenant_id=None,
                policy=_policy(),
            )


def _policy():
    from support_bot.config import Settings
    from support_bot.domain.reminders import ReminderPolicy

    return ReminderPolicy.from_settings(Settings())


async def test_validation_limits(session_factory, make_user) -> None:
    author = await make_user(telegram_id=4, role=UserRole.SUPPORT)
    async with session_factory() as session:
        with pytest.raises(ValidationError):
            await service.create_ticket(
                session,
                actor_id=author.id,
                description="   ",
                priority=TicketPriority.LOW,
                request_id="r",
                tenant_id=None,
                policy=_policy(),
            )
        with pytest.raises(ValidationError):
            await service.create_ticket(
                session,
                actor_id=author.id,
                description="x" * 3001,
                priority=TicketPriority.LOW,
                request_id="r",
                tenant_id=None,
                policy=_policy(),
            )
        with pytest.raises(ValidationError):
            await service.create_ticket(
                session,
                actor_id=author.id,
                description="d",
                priority=TicketPriority.LOW,
                request_id="",
                tenant_id=None,
                policy=_policy(),
            )
        with pytest.raises(ValidationError):
            await service.create_ticket(
                session,
                actor_id=author.id,
                description="d",
                priority=TicketPriority.LOW,
                request_id="r",
                tenant_id="x" * 65,
                policy=_policy(),
            )


async def test_take_by_developer(session_factory, make_user, make_ticket) -> None:
    author = await make_user(telegram_id=10, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=11, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)

    async with session_factory() as session:
        taken = await service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )

    assert taken.status is TicketStatus.IN_PROGRESS
    assert taken.version == 2
    assert taken.assignee_id == developer.id
    assert taken.taken_at is not None
    assert taken.next_reminder_at is None

    async with session_factory() as session:
        events = list(
            (
                await session.scalars(
                    select(TicketEvent)
                    .where(TicketEvent.ticket_id == ticket.id)
                    .order_by(TicketEvent.id)
                )
            ).all()
        )
        entered_at = await service.last_new_event_at(session, ticket.id)
    assert [event.event_type for event in events] == [
        TicketEventType.CREATED,
        TicketEventType.TAKEN,
    ]
    assert events[-1].version == 2
    assert entered_at is not None


async def test_admin_has_developer_and_support_rights(
    session_factory, make_user, make_ticket
) -> None:
    admin = await make_user(telegram_id=35, role=UserRole.ADMIN)
    created = await make_ticket(author_id=admin.id, request_id="ADMIN-1")
    assert created.author_id == admin.id

    async with session_factory() as session:
        taken = await service.take_ticket(
            session, ticket_id=created.id, expected_version=1, actor_id=admin.id
        )
    assert taken.status is TicketStatus.IN_PROGRESS
    assert taken.assignee_id == admin.id


async def test_take_requires_developer_or_admin(session_factory, make_user, make_ticket) -> None:
    author = await make_user(telegram_id=12, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)
    async with session_factory() as session:
        with pytest.raises(PermissionDenied):
            await service.take_ticket(
                session, ticket_id=ticket.id, expected_version=1, actor_id=author.id
            )


async def test_take_conflict_is_atomic(session_factory, make_user, make_ticket) -> None:
    author = await make_user(telegram_id=13, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=14, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)

    async def attempt():
        async with session_factory() as session:
            return await service.take_ticket(
                session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
            )

    results = await asyncio.gather(attempt(), attempt(), return_exceptions=True)
    successes = [result for result in results if isinstance(result, Ticket)]
    errors = [result for result in results if isinstance(result, AlreadyTaken)]
    assert len(successes) == 1
    assert len(errors) == 1

    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.version == 2
    assert current.assignee_id == developer.id


async def test_stale_take_button_after_waiting_cycle(
    session_factory, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=15, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=16, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)

    async with session_factory() as session:
        await service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )
    async with session_factory() as session:
        await service.change_status(
            session,
            ticket_id=ticket.id,
            expected_version=2,
            action=TicketAction.WAIT_INFO,
            actor_id=developer.id,
            policy=_policy(),
        )
    async with session_factory() as session:
        await service.change_status(
            session,
            ticket_id=ticket.id,
            expected_version=3,
            action=TicketAction.TO_WORK,
            actor_id=developer.id,
            policy=_policy(),
        )

    async with session_factory() as session:
        with pytest.raises(AlreadyTaken):
            await service.take_ticket(
                session, ticket_id=ticket.id, expected_version=2, actor_id=developer.id
            )
    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.status is TicketStatus.IN_PROGRESS
    assert current.version == 4
    assert current.assignee_id == developer.id


async def test_only_assignee_or_admin_can_change_status(
    session_factory, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=17, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=18, role=UserRole.DEVELOPER)
    other = await make_user(telegram_id=19, role=UserRole.DEVELOPER)
    admin = await make_user(telegram_id=20, role=UserRole.ADMIN)
    ticket = await make_ticket(author_id=author.id)

    async with session_factory() as session:
        await service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )

    async with session_factory() as session:
        with pytest.raises(PermissionDenied):
            await service.change_status(
                session,
                ticket_id=ticket.id,
                expected_version=2,
                action=TicketAction.RESOLVE,
                actor_id=other.id,
                policy=_policy(),
            )
    async with session_factory() as session:
        resolved = await service.change_status(
            session,
            ticket_id=ticket.id,
            expected_version=2,
            action=TicketAction.RESOLVE,
            actor_id=admin.id,
            policy=_policy(),
        )
    assert resolved.status is TicketStatus.RESOLVED
    assert resolved.closed_at is not None
    assert resolved.assignee_id == developer.id
    assert resolved.version == 3


async def test_cannot_move_new_to_final(session_factory, make_user, make_ticket) -> None:
    author = await make_user(telegram_id=21, role=UserRole.SUPPORT)
    admin = await make_user(telegram_id=22, role=UserRole.ADMIN)
    ticket = await make_ticket(author_id=author.id)
    async with session_factory() as session:
        with pytest.raises(StatusConflict):
            await service.change_status(
                session,
                ticket_id=ticket.id,
                expected_version=1,
                action=TicketAction.RESOLVE,
                actor_id=admin.id,
                policy=_policy(),
            )


async def test_decline_returns_to_new_and_resets_assignment(
    session_factory, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=23, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=24, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id, priority=TicketPriority.HIGH)

    async with session_factory() as session:
        await service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )
    async with session_factory() as session:
        declined = await service.change_status(
            session,
            ticket_id=ticket.id,
            expected_version=2,
            action=TicketAction.DECLINE,
            actor_id=developer.id,
            policy=_policy(),
        )
    assert declined.status is TicketStatus.NEW
    assert declined.assignee_id is None
    assert declined.taken_at is None
    assert declined.closed_at is None
    assert declined.next_reminder_at is not None
    assert declined.version == 3


async def test_reopen_admin_only_and_resets_fields(session_factory, make_user, make_ticket) -> None:
    author = await make_user(telegram_id=25, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=26, role=UserRole.DEVELOPER)
    admin = await make_user(telegram_id=27, role=UserRole.ADMIN)
    ticket = await make_ticket(author_id=author.id)

    async with session_factory() as session:
        await service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )
    async with session_factory() as session:
        await service.change_status(
            session,
            ticket_id=ticket.id,
            expected_version=2,
            action=TicketAction.RESOLVE,
            actor_id=developer.id,
            policy=_policy(),
        )
    async with session_factory() as session:
        with pytest.raises(PermissionDenied):
            await service.reopen_ticket(
                session,
                ticket_id=ticket.id,
                expected_version=3,
                actor_id=developer.id,
                policy=_policy(),
            )
    async with session_factory() as session:
        reopened = await service.reopen_ticket(
            session,
            ticket_id=ticket.id,
            expected_version=3,
            actor_id=admin.id,
            policy=_policy(),
        )
    assert reopened.status is TicketStatus.NEW
    assert reopened.assignee_id is None
    assert reopened.taken_at is None
    assert reopened.closed_at is None
    assert reopened.next_reminder_at is not None
    assert reopened.version == 4


async def test_reassign_admin_only_and_validates_target(
    session_factory, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=28, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=29, role=UserRole.DEVELOPER)
    other_developer = await make_user(telegram_id=30, role=UserRole.DEVELOPER)
    admin = await make_user(telegram_id=31, role=UserRole.ADMIN)
    ticket = await make_ticket(author_id=author.id)

    async with session_factory() as session:
        await service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )

    async with session_factory() as session:
        with pytest.raises(PermissionDenied):
            await service.reassign_ticket(
                session,
                ticket_id=ticket.id,
                expected_version=2,
                actor_id=developer.id,
                new_assignee_id=other_developer.id,
            )
    async with session_factory() as session:
        with pytest.raises(ValidationError):
            await service.reassign_ticket(
                session,
                ticket_id=ticket.id,
                expected_version=2,
                actor_id=admin.id,
                new_assignee_id=author.id,
            )
    async with session_factory() as session:
        reassigned = await service.reassign_ticket(
            session,
            ticket_id=ticket.id,
            expected_version=2,
            actor_id=admin.id,
            new_assignee_id=other_developer.id,
        )
    assert reassigned.status is TicketStatus.IN_PROGRESS
    assert reassigned.assignee_id == other_developer.id
    assert reassigned.taken_at is not None
    assert reassigned.version == 3


async def test_stale_version_is_rejected(session_factory, make_user, make_ticket) -> None:
    author = await make_user(telegram_id=32, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=33, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=author.id)

    async with session_factory() as session:
        with pytest.raises(VersionConflict):
            await service.take_ticket(
                session, ticket_id=ticket.id, expected_version=5, actor_id=developer.id
            )
    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None
    assert current.version == 1
    assert current.status is TicketStatus.NEW


async def test_db_check_constraint_enforces_invariants(
    session_factory, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=34, role=UserRole.SUPPORT)
    ticket = await make_ticket(author_id=author.id)
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(Ticket).where(Ticket.id == ticket.id).values(status=TicketStatus.IN_PROGRESS)
            )
            await session.flush()
        await session.rollback()
