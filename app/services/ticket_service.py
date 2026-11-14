"""Ticket service: business logic for the ticket lifecycle.

Pure business logic — no Telegram or HTTP imports. Every operation takes an
``AsyncSession`` (the caller manages the transaction boundary) and follows the
optimistic-concurrency contract:

* a mutation of an existing ticket is a single conditional ``UPDATE`` guarded
  by ``WHERE id = :id AND version = :expected_version AND status IN (...)``,
  executed in the same transaction as the permission check (fresh role read)
  and the ``TicketEvent`` insert;
* if the conditional ``UPDATE`` matched zero rows, nothing changed and the
  operation raises :class:`TicketChanged` (or :class:`TicketAlreadyTaken` for a
  ``take`` race).

Helpers never commit: the caller (or a thin wrapper) owns the commit.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.enums import Priority, Role, TicketEventType, TicketStatus
from app.db.models import Ticket, TicketEvent, User
from app.services.state_machine import TicketAction, is_action_allowed


class PermissionDenied(Exception):
    """The actor is not allowed to perform the requested action."""


class TicketChanged(Exception):
    """The ticket's version/status no longer matches the caller's expectation."""

    def __init__(self, ticket_id: int | None = None) -> None:
        self.ticket_id = ticket_id
        super().__init__("Обращение изменилось")


class TicketAlreadyTaken(Exception):
    """A ``take`` raced with another actor who already took the ticket."""

    def __init__(self, ticket_id: int | None = None) -> None:
        self.ticket_id = ticket_id
        super().__init__("Уже взято")


def _now() -> datetime:
    return datetime.now(UTC)


async def _actor_role(session: AsyncSession, user_id: int) -> Role | None:
    """Read the user's role fresh from the database (never cached)."""
    return cast(
        Role | None,
        await session.scalar(select(User.role).where(User.id == user_id)),
    )


async def _load_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    return await session.get(Ticket, ticket_id)


async def _conditional_update(
    session: AsyncSession,
    *,
    ticket_id: int,
    expected_version: int,
    expected_statuses: tuple[TicketStatus, ...],
    values: dict[str, Any],
) -> Ticket | None:
    """Run the optimistic-concurrency ``UPDATE ... RETURNING``; ``None`` on miss."""
    result = await session.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.version == expected_version,
            Ticket.status.in_(expected_statuses),
        )
        .values(version=Ticket.version + 1, **values)
        .returning(Ticket),
    )
    return result.scalar_one_or_none()


async def _apply_transition(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    action: TicketAction,
    expected_version: int,
    expected_statuses: tuple[TicketStatus, ...],
    target_status: TicketStatus | None,
    event_type: TicketEventType,
    extra_values: dict[str, Any],
    set_next_reminder: bool = False,
) -> Ticket:
    """Permission check + conditional UPDATE + event insert, atomically.

    ``target_status`` is ``None`` for actions that leave the status unchanged
    (``reassign``). ``set_next_reminder`` recomputes ``next_reminder_at`` from
    the ticket's priority (used when re-entering ``new``).
    """
    role = await _actor_role(session, actor.id)
    ticket = await _load_ticket(session, ticket_id)
    if ticket is None:
        raise TicketChanged(ticket_id)
    if not is_action_allowed(action, ticket.status, role, ticket.assignee_id == actor.id):
        raise PermissionDenied(action.value)

    old_status = ticket.status
    old_assignee_id = ticket.assignee_id

    values = dict(extra_values)
    if set_next_reminder:
        values["next_reminder_at"] = _now() + timedelta(
            seconds=settings.reminder_interval_seconds(ticket.priority)
        )
    if target_status is not None:
        values["status"] = target_status

    updated = await _conditional_update(
        session,
        ticket_id=ticket_id,
        expected_version=expected_version,
        expected_statuses=expected_statuses,
        values=values,
    )
    if updated is None:
        if action is TicketAction.take:
            current_status = cast(
                TicketStatus | None,
                await session.scalar(select(Ticket.status).where(Ticket.id == ticket_id)),
            )
            if current_status is not None and current_status is not TicketStatus.new:
                raise TicketAlreadyTaken(ticket_id)
        raise TicketChanged(ticket_id)

    session.add(
        TicketEvent(
            ticket_id=ticket_id,
            event_type=event_type,
            actor_id=actor.id,
            old_status=old_status.value,
            new_status=updated.status.value,
            old_assignee_id=old_assignee_id,
            new_assignee_id=updated.assignee_id,
            version=updated.version,
        )
    )
    return updated


async def create_ticket(
    session: AsyncSession,
    *,
    author: User,
    priority: Priority,
    description: str,
    request_id: str,
    tenant_id: str | None = None,
) -> Ticket:
    """Create a ticket in status ``new`` (``support`` or ``admin`` only)."""
    role = await _actor_role(session, author.id)
    if role not in (Role.support, Role.admin):
        raise PermissionDenied("create")

    ticket = Ticket(
        author_id=author.id,
        description=description,
        priority=priority,
        request_id=request_id,
        tenant_id=tenant_id,
        status=TicketStatus.new,
        version=1,
        next_reminder_at=_now() + timedelta(seconds=settings.reminder_interval_seconds(priority)),
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type=TicketEventType.created,
            actor_id=author.id,
            old_status=None,
            new_status=TicketStatus.new.value,
            old_assignee_id=None,
            new_assignee_id=None,
            version=1,
        )
    )
    return ticket


async def take(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    expected_version: int,
) -> Ticket:
    """Take a ticket: ``new -> in_progress``, assigning it to ``actor``."""
    return await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.take,
        expected_version=expected_version,
        expected_statuses=(TicketStatus.new,),
        target_status=TicketStatus.in_progress,
        event_type=TicketEventType.taken,
        extra_values={"assignee_id": actor.id, "taken_at": _now(), "next_reminder_at": None},
    )


async def set_waiting_info(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    expected_version: int,
) -> Ticket:
    """Move a ticket ``in_progress -> waiting_info``."""
    return await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.to_waiting,
        expected_version=expected_version,
        expected_statuses=(TicketStatus.in_progress,),
        target_status=TicketStatus.waiting_info,
        event_type=TicketEventType.status_changed,
        extra_values={},
    )


async def set_in_progress(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    expected_version: int,
) -> Ticket:
    """Move a ticket ``waiting_info -> in_progress``."""
    return await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.to_progress,
        expected_version=expected_version,
        expected_statuses=(TicketStatus.waiting_info,),
        target_status=TicketStatus.in_progress,
        event_type=TicketEventType.status_changed,
        extra_values={},
    )


async def _finalize(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    action: TicketAction,
    target_status: TicketStatus,
    expected_version: int,
) -> Ticket:
    return await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=action,
        expected_version=expected_version,
        expected_statuses=(TicketStatus.in_progress, TicketStatus.waiting_info),
        target_status=target_status,
        event_type=TicketEventType.status_changed,
        extra_values={"closed_at": _now()},
    )


async def resolve(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    expected_version: int,
) -> Ticket:
    """Resolve a ticket from ``in_progress``/``waiting_info``."""
    return await _finalize(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.resolve,
        target_status=TicketStatus.resolved,
        expected_version=expected_version,
    )


async def reject(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    expected_version: int,
) -> Ticket:
    """Reject a ticket from ``in_progress``/``waiting_info``."""
    return await _finalize(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.reject,
        target_status=TicketStatus.rejected,
        expected_version=expected_version,
    )


async def mark_duplicate(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    expected_version: int,
) -> Ticket:
    """Mark a ticket as a duplicate from ``in_progress``/``waiting_info``."""
    return await _finalize(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.duplicate,
        target_status=TicketStatus.duplicate,
        expected_version=expected_version,
    )


async def abandon(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    expected_version: int,
) -> Ticket:
    """Abandon a ticket: ``in_progress``/``waiting_info -> new``, unassigning it."""
    return await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.abandon,
        expected_version=expected_version,
        expected_statuses=(TicketStatus.in_progress, TicketStatus.waiting_info),
        target_status=TicketStatus.new,
        event_type=TicketEventType.abandoned,
        extra_values={"assignee_id": None, "taken_at": None},
        set_next_reminder=True,
    )


async def reopen(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    expected_version: int,
) -> Ticket:
    """Reopen a final ticket back to ``new`` (``admin`` only)."""
    return await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.reopen,
        expected_version=expected_version,
        expected_statuses=(TicketStatus.resolved, TicketStatus.rejected, TicketStatus.duplicate),
        target_status=TicketStatus.new,
        event_type=TicketEventType.reopened,
        extra_values={"assignee_id": None, "taken_at": None, "closed_at": None},
        set_next_reminder=True,
    )


async def reassign(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    new_assignee: User,
    expected_version: int,
) -> Ticket:
    """Reassign an active ticket to another ``developer``/``admin`` (``admin`` only)."""
    new_assignee_role = await _actor_role(session, new_assignee.id)
    if new_assignee_role not in (Role.developer, Role.admin):
        raise PermissionDenied("reassign")
    return await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.reassign,
        expected_version=expected_version,
        expected_statuses=(TicketStatus.in_progress, TicketStatus.waiting_info),
        target_status=None,  # status unchanged
        event_type=TicketEventType.reassigned,
        extra_values={"assignee_id": new_assignee.id, "taken_at": _now()},
    )
