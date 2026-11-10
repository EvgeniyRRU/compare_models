from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, TicketEvent, User
from app.domain.state_machine import allowed_transition
from app.metrics import record_after_commit


class Conflict(Exception):
    pass


class Forbidden(Exception):
    pass


INTERVALS = {"critical": 10, "high": 30, "medium": 120, "low": 480}
FINAL = {"resolved", "rejected", "duplicate"}


async def create_ticket(
    session: AsyncSession,
    actor_id: int,
    description: str,
    priority: str,
    request_id: str,
    tenant_id: str | None,
    intervals: dict[str, int] = INTERVALS,
) -> Ticket:
    actor = await session.scalar(select(User).where(User.id == actor_id).with_for_update(read=True))
    if actor is None or actor.role not in {"support", "admin"}:
        raise Forbidden()
    description, request_id = description.strip(), request_id.strip()
    if (
        not 1 <= len(description) <= 3000
        or not 1 <= len(request_id) <= 64
        or priority not in intervals
        or (tenant_id is not None and len(tenant_id) > 64)
    ):
        raise ValueError("Invalid ticket")
    ticket = Ticket(
        author_id=actor.id,
        description=description,
        priority=priority,
        request_id=request_id,
        tenant_id=tenant_id,
        status="new",
        version=1,
        next_reminder_at=datetime.now(UTC) + timedelta(minutes=intervals[priority]),
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=actor.id,
            event_type="created",
            new_status="new",
            version=1,
        )
    )
    record_after_commit(session, "created", priority)
    return ticket


async def transition_ticket(
    session: AsyncSession,
    ticket_id: int,
    actor_id: int,
    expected_status: str,
    expected_version: int,
    target: str,
    intervals: dict[str, int] = INTERVALS,
) -> Ticket:
    actor = await session.scalar(select(User).where(User.id == actor_id).with_for_update(read=True))
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None or ticket.version != expected_version or ticket.status != expected_status:
        raise Conflict()
    if actor is None:
        raise Forbidden()
    allowed_transition(ticket.status, target, actor.role, ticket.assignee_id == actor.id)
    now = datetime.now(UTC)
    old_assignee = ticket.assignee_id
    values = {"status": target, "version": expected_version + 1}
    if target == "in_progress" and expected_status == "new":
        values.update(assignee_id=actor.id, taken_at=now, next_reminder_at=None)
        event_type = "taken"
    elif target == "new":
        values.update(
            assignee_id=None,
            taken_at=None,
            closed_at=None,
            next_reminder_at=now + timedelta(minutes=intervals[ticket.priority]),
        )
        event_type = "reopened" if expected_status in FINAL else "released"
    else:
        if target in FINAL:
            values["closed_at"] = now
        event_type = "status_changed"
    result = await session.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.status == expected_status,
            Ticket.version == expected_version,
        )
        .values(**values)
        .returning(Ticket)
        .execution_options(populate_existing=True)
    )
    changed = result.scalar_one_or_none()
    if changed is None:
        raise Conflict()
    session.add(
        TicketEvent(
            ticket_id=ticket_id,
            actor_id=actor_id,
            event_type=event_type,
            old_status=expected_status,
            new_status=target,
            old_assignee_id=old_assignee,
            new_assignee_id=changed.assignee_id,
            version=changed.version,
        )
    )
    record_after_commit(session, "transition", expected_status, target)
    if event_type == "taken":
        entered = await session.scalar(
            select(TicketEvent.created_at)
            .where(TicketEvent.ticket_id == ticket_id, TicketEvent.new_status == "new")
            .order_by(TicketEvent.version.desc())
            .limit(1)
        )
        if entered:
            record_after_commit(session, "take_time", max(0, (now - entered).total_seconds()))
    return changed


async def reassign_ticket(
    session: AsyncSession,
    ticket_id: int,
    actor_id: int,
    assignee_id: int,
    expected_status: str,
    expected_version: int,
) -> Ticket:
    users = (
        await session.scalars(
            select(User)
            .where(User.id.in_([actor_id, assignee_id]))
            .order_by(User.id)
            .with_for_update(read=True)
        )
    ).all()
    by_id = {user.id: user for user in users}
    if (
        actor_id not in by_id
        or by_id[actor_id].role != "admin"
        or assignee_id not in by_id
        or by_id[assignee_id].role not in {"developer", "admin"}
    ):
        raise Forbidden()
    ticket = await session.get(Ticket, ticket_id)
    if (
        ticket is None
        or ticket.status != expected_status
        or ticket.version != expected_version
        or expected_status not in {"in_progress", "waiting_info"}
    ):
        raise Conflict()
    old_assignee = ticket.assignee_id
    result = await session.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.status == expected_status,
            Ticket.version == expected_version,
        )
        .values(assignee_id=assignee_id, taken_at=datetime.now(UTC), version=expected_version + 1)
        .returning(Ticket)
        .execution_options(populate_existing=True)
    )
    changed = result.scalar_one_or_none()
    if changed is None:
        raise Conflict()
    session.add(
        TicketEvent(
            ticket_id=ticket_id,
            actor_id=actor_id,
            event_type="reassigned",
            old_status=expected_status,
            new_status=expected_status,
            old_assignee_id=old_assignee,
            new_assignee_id=assignee_id,
            version=changed.version,
        )
    )
    return changed
