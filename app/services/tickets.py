"""Обращения: создание и все бизнес-изменения.

Каждое бизнес-изменение существующего обращения — ОДНО условное UPDATE
(WHERE id AND status AND version) в той же транзакции, где проверка прав и
запись в журнал (SPEC «Права»). Technical-записи сюда не входят.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import states
from app.config import Settings
from app.metrics import (
    ticket_take_duration_seconds,
    ticket_transitions_total,
    tickets_created_total,
)
from app.models import Ticket, TicketEvent, User
from app.services.errors import AlreadyTakenError, NotFoundError, PermissionDenied, StaleTicketError


def _now() -> datetime:
    return datetime.now(UTC)


async def load_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise NotFoundError("Обращение не найдено.")
    return ticket


async def _role_of(session: AsyncSession, user_id: int) -> str | None:
    return (await session.execute(select(User.role).where(User.id == user_id))).scalar_one()


async def _lock_user_share(session: AsyncSession, user_id: int) -> None:
    """FOR SHARE строки исполнителя: исключает параллельную смену роли (FOR UPDATE)."""
    await session.execute(text("SELECT id FROM users WHERE id = :id FOR SHARE"), {"id": user_id})


async def create_ticket(
    session: AsyncSession,
    settings: Settings,
    actor_id: int,
    *,
    description: str,
    priority: str,
    request_id: str,
    tenant_id: str | None = None,
) -> Ticket:
    if priority not in states.PRIORITIES:
        raise ValueError(f"Некорректный приоритет: {priority}")
    ticket = Ticket(
        author_id=actor_id,
        description=description,
        priority=priority,
        request_id=request_id,
        tenant_id=tenant_id,
        status="new",
        version=1,
        next_reminder_at=_now() + timedelta(seconds=settings.reminder_interval(priority)),
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type="created",
            actor_id=actor_id,
            new_status="new",
            version=1,
        )
    )
    await session.flush()
    tickets_created_total.labels(priority=priority).inc()
    return ticket


async def take_ticket(
    session: AsyncSession, settings: Settings, actor_id: int, ticket: Ticket, expected_version: int
) -> Ticket:
    """Атомарное «Взять». Гонка: второй нажавший получает AlreadyTakenError."""
    t = states.TRANSITIONS["take"]
    await _lock_user_share(session, actor_id)
    role = await _role_of(session, actor_id)  # читаем ПОСЛЕ блокировки
    states.check_permission(role, actor_id, ticket.assignee_id, t)
    if ticket.status != "new":
        raise AlreadyTakenError()
    result = cast(
        CursorResult[()],
        await session.execute(
            update(Ticket)
            .where(
                Ticket.id == ticket.id, Ticket.status == "new", Ticket.version == expected_version
            )
            .values(
                status="in_progress",
                assignee_id=actor_id,
                taken_at=_now(),
                next_reminder_at=None,
                version=Ticket.version + 1,
            )
        ),
    )
    if result.rowcount != 1:
        await session.refresh(ticket)
        if ticket.status != "new":
            raise AlreadyTakenError()
        raise StaleTicketError()
    await session.refresh(ticket)
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type="taken",
            actor_id=actor_id,
            old_status="new",
            new_status="in_progress",
            old_assignee_id=None,
            new_assignee_id=actor_id,
            version=expected_version + 1,
        )
    )
    await session.flush()
    ticket_transitions_total.labels(from_status="new", to_status="in_progress").inc()
    await _observe_take_duration(session, ticket.id)
    return ticket


async def apply_transition(
    session: AsyncSession,
    settings: Settings,
    actor_id: int,
    ticket: Ticket,
    expected_version: int,
    action: str,
    target_user_id: int | None = None,
) -> Ticket:
    """Все остальные бизнес-переходы (кроме take): один условный UPDATE."""
    t = states.TRANSITIONS.get(action)
    if t is None:
        raise ValueError(f"Неизвестное действие: {action}")
    if ticket.status not in t.from_statuses:
        raise StaleTicketError()

    role = await _role_of(session, actor_id)
    states.check_permission(role, actor_id, ticket.assignee_id, t)

    old_status, old_assignee = ticket.status, ticket.assignee_id
    new_status = t.to_status or ticket.status
    new_assignee = ticket.assignee_id
    values: dict[str, Any] = {"version": Ticket.version + 1}

    if action == "reassign":
        if target_user_id is None:
            raise ValueError("Для reassign требуется target_user_id.")
        await _lock_user_share(session, target_user_id)
        target_role = await _role_of(session, target_user_id)
        if target_role not in states.EXECUTOR_ROLES:
            raise PermissionDenied("Исполнителем может быть только developer или admin.")
        values["assignee_id"] = target_user_id
        values["taken_at"] = _now()
        new_assignee = target_user_id
    else:
        if t.to_status is not None:
            values["status"] = t.to_status
        if t.sets_taken_at:
            values["taken_at"] = _now()
        if t.clears_taken_at:
            values["taken_at"] = None
        if t.sets_closed_at:
            values["closed_at"] = _now()
        if t.clears_closed_at:
            values["closed_at"] = None
        if t.clears_assignee:
            values["assignee_id"] = None
            new_assignee = None
        if t.sets_reminder:
            values["next_reminder_at"] = _now() + timedelta(
                seconds=settings.reminder_interval(ticket.priority)
            )
        if t.clears_reminder:
            values["next_reminder_at"] = None

    result = cast(
        CursorResult[()],
        await session.execute(
            update(Ticket)
            .where(
                Ticket.id == ticket.id,
                Ticket.status == old_status,
                Ticket.version == expected_version,
            )
            .values(**values)
        ),
    )
    if result.rowcount != 1:
        raise StaleTicketError()
    await session.refresh(ticket)
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type=t.event_type,
            actor_id=actor_id,
            old_status=old_status,
            new_status=new_status,
            old_assignee_id=old_assignee,
            new_assignee_id=new_assignee,
            version=expected_version + 1,
        )
    )
    await session.flush()
    ticket_transitions_total.labels(from_status=old_status, to_status=new_status).inc()
    return ticket


async def _observe_take_duration(session: AsyncSession, ticket_id: int) -> None:
    """Время до взятия: от последнего входа в new (из журнала) до взятия."""
    last_new_at = (
        await session.execute(
            text(
                "SELECT created_at FROM ticket_events "
                "WHERE ticket_id = :id AND new_status = 'new' ORDER BY id DESC LIMIT 1"
            ),
            {"id": ticket_id},
        )
    ).scalar_one()
    ticket_take_duration_seconds.observe((_now() - last_new_at).total_seconds())
