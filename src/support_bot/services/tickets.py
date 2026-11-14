from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from support_bot.db.enums import (
    FINAL_STATUSES,
    TicketEventType,
    TicketPriority,
    TicketStatus,
    UserRole,
)
from support_bot.db.models.ticket import Ticket
from support_bot.db.models.ticket_event import TicketEvent
from support_bot.db.models.user import User
from support_bot.domain.errors import (
    AlreadyTaken,
    PermissionDenied,
    TicketNotFound,
    UserNotFound,
    ValidationError,
    VersionConflict,
)
from support_bot.domain.reminders import ReminderPolicy
from support_bot.domain.state_machine import (
    RULES,
    TicketAction,
    check_permission,
    rule_for,
)
from support_bot.observability.metrics import (
    observe_ticket_created,
    observe_time_to_take,
    observe_transition,
)

MAX_DESCRIPTION_LENGTH = 3000
MAX_ID_LENGTH = 64


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class TicketInput:
    description: str
    priority: TicketPriority
    request_id: str
    tenant_id: str | None


def validate_ticket_input(
    *,
    description: str,
    priority: TicketPriority,
    request_id: str,
    tenant_id: str | None,
) -> TicketInput:
    description = description.strip()
    if not description:
        raise ValidationError("Описание не может быть пустым")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValidationError(f"Описание не может быть длиннее {MAX_DESCRIPTION_LENGTH} символов")

    request_id = request_id.strip()
    if not request_id:
        raise ValidationError("ID заявки не может быть пустым")
    if len(request_id) > MAX_ID_LENGTH:
        raise ValidationError(f"ID заявки не может быть длиннее {MAX_ID_LENGTH} символов")

    if tenant_id is not None:
        tenant_id = tenant_id.strip()
        if not tenant_id:
            raise ValidationError("ID арендатора не может быть пустым")
        if len(tenant_id) > MAX_ID_LENGTH:
            raise ValidationError(f"ID арендатора не может быть длиннее {MAX_ID_LENGTH} символов")

    return TicketInput(
        description=description,
        priority=priority,
        request_id=request_id,
        tenant_id=tenant_id,
    )


async def _role(session: AsyncSession, user_id: int, *, for_share: bool = False) -> UserRole | None:
    stmt = select(User.role).where(User.id == user_id)
    if for_share:
        stmt = stmt.with_for_update(read=True)
    return await session.scalar(stmt)


async def _load_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketNotFound(str(ticket_id))
    return ticket


async def _conditional_update(
    session: AsyncSession,
    ticket: Ticket,
    expected_version: int,
    values: dict[str, object],
) -> bool:
    stmt = (
        update(Ticket)
        .where(
            Ticket.id == ticket.id,
            Ticket.version == expected_version,
            Ticket.status == ticket.status,
        )
        .values(**values, version=expected_version + 1)
        .returning(Ticket.id)
    )
    updated_id = await session.scalar(stmt)
    if updated_id is None:
        return False
    await session.refresh(ticket)
    return True


def _add_event(
    session: AsyncSession,
    *,
    ticket: Ticket,
    event_type: TicketEventType,
    actor_id: int,
    old_status: TicketStatus,
    old_assignee_id: int | None,
) -> None:
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type=event_type,
            actor_id=actor_id,
            old_status=old_status,
            new_status=ticket.status,
            old_assignee_id=old_assignee_id,
            new_assignee_id=ticket.assignee_id,
            version=ticket.version,
        )
    )


async def _insert_ticket(
    session: AsyncSession,
    *,
    actor_id: int,
    data: TicketInput,
    policy: ReminderPolicy,
    now: datetime,
) -> Ticket:
    ticket = Ticket(
        author_id=actor_id,
        assignee_id=None,
        description=data.description,
        priority=data.priority,
        request_id=data.request_id,
        tenant_id=data.tenant_id,
        status=TicketStatus.NEW,
        version=1,
        taken_at=None,
        closed_at=None,
        next_reminder_at=now + policy.interval_for(data.priority),
        message_id=None,
        post_synced_version=0,
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type=TicketEventType.CREATED,
            actor_id=actor_id,
            old_status=None,
            new_status=TicketStatus.NEW,
            old_assignee_id=None,
            new_assignee_id=None,
            version=1,
        )
    )
    return ticket


async def create_ticket(
    session: AsyncSession,
    *,
    actor_id: int,
    description: str,
    priority: TicketPriority,
    request_id: str,
    tenant_id: str | None,
    policy: ReminderPolicy,
) -> Ticket:
    data = validate_ticket_input(
        description=description,
        priority=priority,
        request_id=request_id,
        tenant_id=tenant_id,
    )
    now = utcnow()
    async with session.begin():
        role = await _role(session, actor_id)
        if role not in (UserRole.SUPPORT, UserRole.ADMIN):
            raise PermissionDenied("support or admin role required")
        ticket = await _insert_ticket(session, actor_id=actor_id, data=data, policy=policy, now=now)
    observe_ticket_created(priority)
    return ticket


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    return await _load_ticket(session, ticket_id)


async def get_ticket_with_relations(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await session.scalar(
        select(Ticket)
        .options(joinedload(Ticket.author), joinedload(Ticket.assignee))
        .where(Ticket.id == ticket_id)
    )
    if ticket is None:
        raise TicketNotFound(str(ticket_id))
    return ticket


async def last_new_event_at(session: AsyncSession, ticket_id: int) -> datetime | None:
    return await session.scalar(
        select(TicketEvent.created_at)
        .where(
            TicketEvent.ticket_id == ticket_id,
            TicketEvent.new_status == TicketStatus.NEW,
        )
        .order_by(TicketEvent.id.desc())
        .limit(1)
    )


async def take_ticket(
    session: AsyncSession,
    *,
    ticket_id: int,
    expected_version: int,
    actor_id: int,
) -> Ticket:
    now = utcnow()
    async with session.begin():
        actor = (
            await session.execute(
                select(User).where(User.id == actor_id).with_for_update(read=True)
            )
        ).scalar_one_or_none()
        if actor is None:
            raise PermissionDenied("actor not found")
        check_permission(TicketAction.TAKE, RULES[TicketAction.TAKE], actor.role, actor_id, None)

        ticket = await _load_ticket(session, ticket_id)
        if ticket.status != TicketStatus.NEW:
            raise AlreadyTaken()
        if ticket.version != expected_version:
            raise VersionConflict()

        updated = await _conditional_update(
            session,
            ticket,
            expected_version,
            {
                "status": TicketStatus.IN_PROGRESS,
                "assignee_id": actor_id,
                "taken_at": now,
                "closed_at": None,
                "next_reminder_at": None,
            },
        )
        if not updated:
            await session.refresh(ticket)
            if ticket.status != TicketStatus.NEW:
                raise AlreadyTaken()
            raise VersionConflict()

        entered_at = await last_new_event_at(session, ticket_id)
        _add_event(
            session,
            ticket=ticket,
            event_type=TicketEventType.TAKEN,
            actor_id=actor_id,
            old_status=TicketStatus.NEW,
            old_assignee_id=None,
        )
        observe_transition(TicketStatus.NEW, TicketStatus.IN_PROGRESS)
    if entered_at is not None:
        observe_time_to_take((now - entered_at).total_seconds())
    return ticket


async def change_status(
    session: AsyncSession,
    *,
    ticket_id: int,
    expected_version: int,
    action: TicketAction,
    actor_id: int,
    policy: ReminderPolicy,
) -> Ticket:
    if action not in (
        TicketAction.WAIT_INFO,
        TicketAction.TO_WORK,
        TicketAction.RESOLVE,
        TicketAction.REJECT,
        TicketAction.DUPLICATE,
        TicketAction.DECLINE,
    ):
        raise ValueError(f"unsupported action {action}")

    now = utcnow()
    async with session.begin():
        role = await _role(session, actor_id)
        ticket = await _load_ticket(session, ticket_id)
        rule = rule_for(ticket.status, action)
        check_permission(action, rule, role, actor_id, ticket.assignee_id)
        if ticket.version != expected_version:
            raise VersionConflict()

        old_status = ticket.status
        old_assignee_id = ticket.assignee_id
        values: dict[str, object] = {}
        if rule.to_status is not None:
            values["status"] = rule.to_status
        if action is TicketAction.DECLINE:
            values.update(
                {
                    "assignee_id": None,
                    "taken_at": None,
                    "closed_at": None,
                    "next_reminder_at": now + policy.interval_for(ticket.priority),
                }
            )
        elif rule.to_status in FINAL_STATUSES:
            values.update({"closed_at": now, "next_reminder_at": None})

        if not await _conditional_update(session, ticket, expected_version, values):
            raise VersionConflict()

        event_type = (
            TicketEventType.DECLINED
            if action is TicketAction.DECLINE
            else TicketEventType.STATUS_CHANGED
        )
        _add_event(
            session,
            ticket=ticket,
            event_type=event_type,
            actor_id=actor_id,
            old_status=old_status,
            old_assignee_id=old_assignee_id,
        )

    observe_transition(old_status, ticket.status)
    return ticket


async def reopen_ticket(
    session: AsyncSession,
    *,
    ticket_id: int,
    expected_version: int,
    actor_id: int,
    policy: ReminderPolicy,
) -> Ticket:
    now = utcnow()
    async with session.begin():
        role = await _role(session, actor_id)
        ticket = await _load_ticket(session, ticket_id)
        rule = rule_for(ticket.status, TicketAction.REOPEN)
        check_permission(TicketAction.REOPEN, rule, role, actor_id, ticket.assignee_id)
        if ticket.version != expected_version:
            raise VersionConflict()

        old_status = ticket.status
        old_assignee_id = ticket.assignee_id
        updated = await _conditional_update(
            session,
            ticket,
            expected_version,
            {
                "status": TicketStatus.NEW,
                "assignee_id": None,
                "taken_at": None,
                "closed_at": None,
                "next_reminder_at": now + policy.interval_for(ticket.priority),
            },
        )
        if not updated:
            raise VersionConflict()

        _add_event(
            session,
            ticket=ticket,
            event_type=TicketEventType.REOPENED,
            actor_id=actor_id,
            old_status=old_status,
            old_assignee_id=old_assignee_id,
        )

    observe_transition(old_status, TicketStatus.NEW)
    return ticket


async def reassign_ticket(
    session: AsyncSession,
    *,
    ticket_id: int,
    expected_version: int,
    actor_id: int,
    new_assignee_id: int,
) -> Ticket:
    now = utcnow()
    async with session.begin():
        locked_ids = sorted({actor_id, new_assignee_id})
        users = list(
            (
                await session.scalars(
                    select(User)
                    .where(User.id.in_(locked_ids))
                    .order_by(User.id)
                    .with_for_update(read=True)
                )
            ).all()
        )
        by_id = {user.id: user for user in users}
        actor = by_id.get(actor_id)
        target = by_id.get(new_assignee_id)
        if actor is None or target is None:
            raise UserNotFound("actor or assignee not found")
        if actor.role != UserRole.ADMIN:
            raise PermissionDenied("admin role required")
        if target.role not in (UserRole.DEVELOPER, UserRole.ADMIN):
            raise ValidationError("исполнитель должен иметь роль developer или admin")

        ticket = await _load_ticket(session, ticket_id)
        rule = rule_for(ticket.status, TicketAction.REASSIGN)
        check_permission(TicketAction.REASSIGN, rule, actor.role, actor_id, ticket.assignee_id)
        if ticket.version != expected_version:
            raise VersionConflict()

        old_status = ticket.status
        old_assignee_id = ticket.assignee_id
        updated = await _conditional_update(
            session,
            ticket,
            expected_version,
            {"assignee_id": new_assignee_id, "taken_at": now},
        )
        if not updated:
            raise VersionConflict()

        _add_event(
            session,
            ticket=ticket,
            event_type=TicketEventType.REASSIGNED,
            actor_id=actor_id,
            old_status=old_status,
            old_assignee_id=old_assignee_id,
        )

    return ticket
