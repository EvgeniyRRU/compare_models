"""Ticket business operations.

Every business change is one conditional UPDATE (id + expected version + expected
status) inside the caller's transaction, plus one ticket_events row. Permissions are
checked against the actor's role freshly read in the same transaction. Row locks:
ticket row FOR UPDATE; executor (assignee) row FOR SHARE for take/reassign so that a
concurrent role demotion (FOR UPDATE on the user) cannot slip between the check and
the assignment.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import func, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.config import Settings
from app.db.models import (
    EventType,
    Priority,
    Ticket,
    TicketEvent,
    TicketStatus,
    User,
)
from app.domain import state_machine as sm
from app.domain.errors import (
    AlreadyTaken,
    Forbidden,
    InvalidTransition,
    NotFound,
    ValidationError,
    VersionConflict,
)
from app.domain.state_machine import Action
from app.logging_setup import get_logger

log = get_logger("services.tickets")

MAX_DESCRIPTION = 3000
MAX_ID_FIELD = 64


@dataclass
class TicketView:
    """Service-layer DTO returned to thin adapters (bot handlers / REST)."""

    ticket: Ticket
    author: User | None
    assignee: User | None


def _interval_expr(priority: str, settings: Settings) -> object:
    minutes = settings.reminder_interval_minutes(priority)
    # Named PG argument syntax: positional make_interval() args are order-sensitive and error-prone.
    return text("now() + make_interval(mins => :reminder_minutes)").bindparams(
        reminder_minutes=minutes
    )


def validate_ticket_fields(
    description: str | None, request_id: str | None, tenant_id: str | None
) -> tuple[str, str, str | None]:
    desc = (description or "").strip()
    if not desc:
        raise ValidationError("Описание не может быть пустым")
    if len(desc) > MAX_DESCRIPTION:
        raise ValidationError(f"Описание длиннее {MAX_DESCRIPTION} символов")
    rid = (request_id or "").strip()
    if not rid:
        raise ValidationError("ID заявки обязателен")
    if len(rid) > MAX_ID_FIELD:
        raise ValidationError(f"ID заявки длиннее {MAX_ID_FIELD} символов")
    tid = (tenant_id or "").strip() or None
    if tid is not None and len(tid) > MAX_ID_FIELD:
        raise ValidationError(f"ID арендатора длиннее {MAX_ID_FIELD} символов")
    return desc, rid, tid


async def create(
    session: AsyncSession,
    settings: Settings,
    *,
    author_id: int,
    description: str,
    priority: str,
    request_id: str,
    tenant_id: str | None,
) -> Ticket:
    """INSERT ticket + created event in one transaction.

    Caller must have verified the creator's role (support/admin) freshly from DB.
    """
    desc, rid, tid = validate_ticket_fields(description, request_id, tenant_id)
    try:
        prio = Priority(priority)
    except ValueError:
        raise ValidationError("Неизвестный приоритет") from None
    ticket = Ticket(
        author_id=author_id,
        description=desc,
        priority=prio,
        request_id=rid,
        tenant_id=tid,
        status=TicketStatus.new,
        version=1,
    )
    session.add(ticket)
    await session.flush()
    # Reminder arm: DB-clock based, same transaction (spec: "при создании ... ставится").
    await session.execute(
        update(Ticket)
        .where(Ticket.id == ticket.id)
        .values(next_reminder_at=_interval_expr(str(prio), settings))
    )
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type=EventType.created,
            actor_id=author_id,
            new_status=TicketStatus.new,
            version_after=1,
        )
    )
    await session.flush()
    metrics.TICKETS_CREATED.labels(str(prio)).inc()
    log.info("ticket_created", component="tickets", ticket_id=ticket.id)
    await session.refresh(ticket)
    return ticket


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.get(Ticket, ticket_id)


async def _lock_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = (
        await session.execute(
            select(Ticket).where(Ticket.id == ticket_id).with_for_update()
        )
    ).scalar_one_or_none()
    if ticket is None:
        raise NotFound()
    return ticket


async def _observe_time_to_take(session: AsyncSession, ticket: Ticket) -> None:
    seconds = await session.scalar(
        text(
            "SELECT EXTRACT(EPOCH FROM (now() - created_at)) FROM ticket_events "
            "WHERE ticket_id = :t AND event_type IN ('created', 'abandoned', 'reopened') "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"t": ticket.id},
    )
    if seconds is not None:
        metrics.TICKET_TIME_TO_TAKE.labels(str(ticket.priority)).observe(float(seconds))


async def _apply(
    session: AsyncSession,
    settings: Settings,
    *,
    ticket_id: int,
    actor_id: int,
    action: Action,
    expected_version: int,
    target_assignee_id: int | None = None,
) -> Ticket:
    spec = sm.spec(action)
    ticket = await _lock_ticket(session, ticket_id)

    # Actor role: fresh read, no caching.
    actor_role = await session.scalar(
        select(User.role).where(User.telegram_id == actor_id)
    )
    if actor_role is None:
        raise Forbidden("Нет доступа")

    if not sm.is_transition_allowed(action, ticket.status):
        if action is Action.take:
            raise AlreadyTaken()
        raise InvalidTransition()
    if not sm.can_actor(action, actor_role, actor_id, ticket.assignee_id):
        raise Forbidden("Недостаточно прав для этого действия")

    if action is Action.take:
        # Lock the future executor row FOR SHARE and re-read role under the lock.
        executor = await session.scalar(
            select(User).where(User.telegram_id == actor_id).with_for_update(read=True)
        )
        if not (executor and executor.role and executor.role.can_execute):
            raise Forbidden("Нужна роль developer или admin")
    if action is Action.reassign:
        assert target_assignee_id is not None
        executor = await session.scalar(
            select(User)
            .where(User.telegram_id == target_assignee_id)
            .with_for_update(read=True)
        )
        if not (executor and executor.role and executor.role.can_execute):
            raise ValidationError("Переназначить можно только developer или admin")

    if ticket.version != expected_version:
        raise VersionConflict()

    now = func.now()
    old_status = ticket.status
    old_version = ticket.version
    values: dict[str, object] = {
        "version": old_version + 1,
        "status": spec.target_status or old_status,
    }
    old_assignee = ticket.assignee_id
    new_assignee = ticket.assignee_id
    if action is Action.take:
        values.update(assignee_id=actor_id, taken_at=now, next_reminder_at=None)
        new_assignee = actor_id
    elif action is Action.reassign:
        assert target_assignee_id is not None
        values.update(assignee_id=target_assignee_id, taken_at=now)
        new_assignee = target_assignee_id
    elif spec.target_status in (
        TicketStatus.resolved,
        TicketStatus.rejected,
        TicketStatus.duplicate,
    ):
        values.update(closed_at=now)
    elif action is Action.abandon:
        values.update(
            assignee_id=None,
            taken_at=None,
            next_reminder_at=_interval_expr(str(ticket.priority), settings),
        )
        new_assignee = None
    elif action is Action.reopen:
        values.update(
            assignee_id=None,
            taken_at=None,
            closed_at=None,
            next_reminder_at=_interval_expr(str(ticket.priority), settings),
        )
        new_assignee = None

    result = cast(
        CursorResult[tuple[Any, ...]],
        await session.execute(
            update(Ticket)
            .where(
                Ticket.id == ticket_id,
                Ticket.version == expected_version,
                Ticket.status == ticket.status,
            )
            .values(**values)
        )
    )
    if result.rowcount != 1:  # pragma: no cover - protected by FOR UPDATE + version check
        raise VersionConflict()

    session.add(
        TicketEvent(
            ticket_id=ticket_id,
            event_type=spec.event_type,
            actor_id=actor_id,
            old_status=old_status,
            new_status=spec.target_status or old_status,
            old_assignee_id=old_assignee,
            new_assignee_id=new_assignee,
            version_after=old_version + 1,
        )
    )
    await session.flush()

    final_status = spec.target_status or old_status
    metrics.TICKET_TRANSITIONS.labels(str(old_status), str(final_status)).inc()
    if action is Action.take:
        await _observe_time_to_take(session, ticket)

    log.info(
        "ticket_changed",
        component="tickets",
        ticket_id=ticket_id,
        action=str(action),
        version_after=old_version + 1,
    )
    ticket = await _lock_ticket(session, ticket_id)  # fresh state after UPDATE
    return ticket


async def take(
    session: AsyncSession,
    settings: Settings,
    *,
    ticket_id: int,
    actor_id: int,
    expected_version: int,
) -> Ticket:
    return await _apply(
        session, settings, ticket_id=ticket_id, actor_id=actor_id,
        action=Action.take, expected_version=expected_version,
    )


async def apply_action(
    session: AsyncSession,
    settings: Settings,
    *,
    ticket_id: int,
    actor_id: int,
    action: Action,
    expected_version: int,
    target_assignee_id: int | None = None,
) -> Ticket:
    assert action is not Action.take
    return await _apply(
        session, settings, ticket_id=ticket_id, actor_id=actor_id,
        action=action, expected_version=expected_version,
        target_assignee_id=target_assignee_id,
    )


async def last_new_entry_at(session: AsyncSession, ticket_id: int) -> datetime | None:
    """When the ticket last entered `new` (creation/abandon/reopen) — from the journal."""
    return await session.scalar(
        text(
            "SELECT created_at FROM ticket_events "
            "WHERE ticket_id = :t AND event_type IN ('created', 'abandoned', 'reopened') "
            "ORDER BY id DESC LIMIT 1"
        ),
        {"t": ticket_id},
    )
