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
from app.metrics import status_transitions, ticket_time_to_take, tickets_created
from app.services.state_machine import (
    TicketAction,
    is_action_allowed,
    source_statuses,
    target_status,
)


class PermissionDenied(Exception):
    """The actor is not allowed to perform the requested action."""

    def __init__(self, action: str, message: str = "Недостаточно прав") -> None:
        self.action = action
        super().__init__(f"{message}: {action}")


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


def _next_reminder_at(priority: Priority) -> datetime:
    """Compute the next reminder deadline for a ticket of the given priority."""
    return _now() + timedelta(seconds=settings.reminder_interval_seconds(priority))


async def _actor_role(session: AsyncSession, user_id: int) -> Role | None:
    """Read the user's role fresh from the database (never cached)."""
    return cast(
        Role | None,
        await session.scalar(select(User.role).where(User.id == user_id)),
    )


async def _lock_user_for_share(session: AsyncSession, user_id: int) -> Role | None:
    """``SELECT ... FOR SHARE`` the user row and return its role.

    ``FOR SHARE`` blocks a concurrent ``FOR UPDATE`` (a role change in
    :mod:`app.services.role_service`) from committing between this role read and
    the ticket's conditional ``UPDATE``, closing the race where a role downgrade
    lands on an assignee mid-transition. The lock is held until the caller
    commits (or rolls back) the surrounding transaction.
    """
    return cast(
        Role | None,
        await session.scalar(
            select(User.role).where(User.id == user_id).with_for_update(read=True)
        ),
    )


async def _load_ticket(session: AsyncSession, ticket_id: int) -> Ticket | None:
    # ``populate_existing=True`` forces a fresh SELECT so the ``status`` and
    # ``assignee_id`` used for the permission check are read from the same
    # row/version that the conditional UPDATE below guards against.
    return await session.get(Ticket, ticket_id, populate_existing=True)


async def _conditional_update(
    session: AsyncSession,
    *,
    ticket_id: int,
    expected_version: int,
    expected_statuses: set[TicketStatus],
    values: dict[str, Any],
) -> Ticket | None:
    """Run the optimistic-concurrency ``UPDATE ... RETURNING``; ``None`` on miss."""
    result = await session.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.version == expected_version,
            Ticket.status.in_(tuple(expected_statuses)),
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
    event_type: TicketEventType,
    extra_values: dict[str, Any],
    set_next_reminder: bool = False,
) -> Ticket:
    """Permission check + conditional UPDATE + event insert, atomically.

    The source/target statuses are derived from the transition table in
    :mod:`app.services.state_machine` (``source_statuses``/``target_status``),
    so the service never duplicates the state machine's graph.
    ``set_next_reminder`` recomputes ``next_reminder_at`` from the ticket's
    priority (used when re-entering ``new``).
    """
    role = await _actor_role(session, actor.id)
    ticket = await _load_ticket(session, ticket_id)
    if ticket is None:
        raise TicketChanged(ticket_id)

    # Invariant: the permission read is consistent with the guarded version.
    # ``_load_ticket`` reads with ``populate_existing=True``, so ``ticket.status``
    # and ``ticket.assignee_id`` reflect the row at the same ``version`` that
    # ``_conditional_update`` guards with ``version = expected_version``. If that
    # row has since changed, the guarded UPDATE matches zero rows and we fail
    # without committing.
    if not is_action_allowed(action, ticket.status, role, ticket.assignee_id == actor.id):
        raise PermissionDenied(action.value)

    old_status = ticket.status
    old_assignee_id = ticket.assignee_id

    values = dict(extra_values)
    if set_next_reminder:
        values["next_reminder_at"] = _next_reminder_at(ticket.priority)
    target = target_status(action)
    if target is not None:
        values["status"] = target

    updated = await _conditional_update(
        session,
        ticket_id=ticket_id,
        expected_version=expected_version,
        expected_statuses=source_statuses(action),
        values=values,
    )
    if updated is None:
        # ``take`` is the only action whose stale-button failure is surfaced as
        # "already taken" rather than a generic "changed". Its permission is
        # role-only (not assignee-dependent), so a 0-row miss here cannot be a
        # permission problem: re-read the current status to distinguish the two.
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
    status_transitions.labels(old_status.value, updated.status.value).inc()
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
        next_reminder_at=_next_reminder_at(priority),
    )
    session.add(ticket)
    await session.flush()
    tickets_created.labels(priority=ticket.priority.value).inc()
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
    # The taker becomes the assignee: lock their user row FOR SHARE so a
    # concurrent role downgrade (FOR UPDATE) cannot commit until this ticket's
    # conditional UPDATE is done.
    await _lock_user_for_share(session, actor.id)
    ticket = await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.take,
        expected_version=expected_version,
        event_type=TicketEventType.taken,
        extra_values={"assignee_id": actor.id, "taken_at": _now(), "next_reminder_at": None},
    )

    # Measure time-to-take: find the most recent journal entry that placed the
    # ticket into ``new`` and compute the elapsed wall-clock time.
    last_new_event = await session.scalar(
        select(TicketEvent)
        .where(
            TicketEvent.ticket_id == ticket_id,
            TicketEvent.event_type.in_(
                [TicketEventType.created, TicketEventType.abandoned, TicketEventType.reopened],
            ),
            TicketEvent.new_status == TicketStatus.new.value,
        )
        .order_by(TicketEvent.created_at.desc())
        .limit(1),
    )
    if last_new_event is not None:
        delta = (_now() - last_new_event.created_at).total_seconds()
        ticket_time_to_take.observe(delta)

    return ticket


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
        event_type=TicketEventType.status_changed,
        extra_values={},
    )


async def _finalize(
    session: AsyncSession,
    *,
    ticket_id: int,
    actor: User,
    action: TicketAction,
    expected_version: int,
) -> Ticket:
    return await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=action,
        expected_version=expected_version,
        event_type=TicketEventType.status_changed,
        extra_values={"closed_at": _now(), "next_reminder_at": None},
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
    # Lock the new assignee's row FOR SHARE and read their role atomically: this
    # blocks a concurrent role downgrade (FOR UPDATE) from landing mid-reassign.
    new_assignee_role = await _lock_user_for_share(session, new_assignee.id)
    if new_assignee_role not in (Role.developer, Role.admin):
        raise PermissionDenied("reassign")
    return await _apply_transition(
        session,
        ticket_id=ticket_id,
        actor=actor,
        action=TicketAction.reassign,
        expected_version=expected_version,
        event_type=TicketEventType.reassigned,
        extra_values={"assignee_id": new_assignee.id, "taken_at": _now()},
    )
