from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.config import Settings
from support_bot.db.models import PRIORITIES, Ticket, TicketEvent
from support_bot.domain.errors import (
    AlreadyTaken,
    Forbidden,
    NotFound,
    StaleTicket,
    ValidationFailed,
)
from support_bot.domain.state_machine import Action, Status, decide
from support_bot.services.users import current_role
from support_bot.services.validation import DESCRIPTION_MAX, ID_MAX, optional_text, required_text

EVENT_TYPES = {
    Action.TAKE: "taken",
    Action.RELEASE: "released",
    Action.REASSIGN: "reassigned",
    Action.REOPEN: "reopened",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def create_ticket(
    session: AsyncSession,
    *,
    settings: Settings,
    actor_id: int,
    description: str,
    priority: str,
    request_id: str,
    tenant_id: str | None,
    now: datetime | None = None,
) -> Ticket:
    now = now or _utc_now()
    role = await current_role(session, actor_id, share_lock=True)
    if role is None or not role.can_create:
        raise Forbidden("Создавать обращения могут поддержка и администраторы")
    if priority not in PRIORITIES:
        raise ValidationFailed("Неизвестный приоритет")

    ticket = Ticket(
        author_id=actor_id,
        description=required_text(description, field="Описание", max_length=DESCRIPTION_MAX),
        priority=priority,
        request_id=required_text(request_id, field="ID заявки", max_length=ID_MAX),
        tenant_id=optional_text(tenant_id, field="ID арендатора", max_length=ID_MAX),
        status=Status.NEW.value,
        version=1,
        post_synced_version=0,
        next_reminder_at=now + timedelta(minutes=settings.reminder_interval_minutes(priority)),
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            event_type="created",
            actor_id=actor_id,
            from_status=None,
            to_status=Status.NEW.value,
            from_assignee_id=None,
            to_assignee_id=None,
            version=1,
            created_at=now,
        )
    )
    return ticket


async def get_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    if ticket is None:
        raise NotFound("Обращение не найдено")
    return ticket


async def transition(
    session: AsyncSession,
    *,
    settings: Settings,
    actor_id: int,
    ticket_id: int,
    action: Action,
    expected_version: int,
    expected_status: Status,
    target_user_id: int | None = None,
    now: datetime | None = None,
) -> Ticket:
    """Apply one business action under the optimistic version check.

    The caller owns the transaction. Permission checks and the conditional UPDATE
    run in the same transaction; any mismatch raises and changes nothing.
    """
    now = now or _utc_now()
    actor_role = await current_role(session, actor_id, share_lock=True)
    if actor_role is None:
        raise Forbidden("Нет доступа")

    ticket = await get_ticket(session, ticket_id)
    if ticket.version != expected_version or ticket.status != expected_status:
        raise _stale_error(action, ticket)

    is_assignee = ticket.assignee_id == actor_id
    to_status = decide(expected_status, action, actor_role=actor_role, is_assignee=is_assignee)

    new_assignee = ticket.assignee_id
    if action is Action.TAKE:
        new_assignee = actor_id
    elif action is Action.REASSIGN:
        new_assignee = await _assignable_target(session, target_user_id)
    elif to_status is Status.NEW:
        new_assignee = None

    taken_at = ticket.taken_at
    if action in (Action.TAKE, Action.REASSIGN):
        taken_at = now
    elif to_status is Status.NEW:
        taken_at = None

    closed_at = now if to_status.is_final else None
    next_reminder_at = None
    if to_status is Status.NEW:
        next_reminder_at = now + timedelta(
            minutes=settings.reminder_interval_minutes(ticket.priority)
        )

    result = await session.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.version == expected_version,
            Ticket.status == expected_status.value,
        )
        .values(
            status=to_status.value,
            assignee_id=new_assignee,
            taken_at=taken_at,
            closed_at=closed_at,
            next_reminder_at=next_reminder_at,
            version=Ticket.version + 1,
        )
        .returning(Ticket.version)
    )
    new_version = result.scalar_one_or_none()
    if new_version is None:
        current = await get_ticket(session, ticket_id)
        raise _stale_error(action, current)

    session.add(
        TicketEvent(
            ticket_id=ticket_id,
            event_type=EVENT_TYPES.get(action, "status_changed"),
            actor_id=actor_id,
            from_status=expected_status.value,
            to_status=to_status.value,
            from_assignee_id=ticket.assignee_id,
            to_assignee_id=new_assignee,
            version=new_version,
            created_at=now,
        )
    )
    await session.flush()
    return await get_ticket(session, ticket_id)


async def transition_at_version(
    session: AsyncSession,
    *,
    settings: Settings,
    actor_id: int,
    ticket_id: int,
    action: Action,
    expected_version: int,
    target_user_id: int | None = None,
    now: datetime | None = None,
) -> Ticket:
    """Button entry point: a button carries the version, the status comes from the DB.

    Each business change bumps the version, so a version identifies one status.
    `transition` still re-checks both in its conditional UPDATE.
    """
    ticket = await get_ticket(session, ticket_id)
    if ticket.version != expected_version:
        raise _stale_error(action, ticket)
    return await transition(
        session,
        settings=settings,
        actor_id=actor_id,
        ticket_id=ticket_id,
        action=action,
        expected_version=expected_version,
        expected_status=Status(ticket.status),
        target_user_id=target_user_id,
        now=now,
    )


def _stale_error(action: Action, ticket: Ticket) -> Exception:
    if action is Action.TAKE and ticket.status != Status.NEW.value:
        return AlreadyTaken("Уже взято")
    return StaleTicket("Обращение изменилось")


async def _assignable_target(session: AsyncSession, target_user_id: int | None) -> int:
    if target_user_id is None:
        raise ValidationFailed("Не выбран исполнитель")
    role = await current_role(session, target_user_id, share_lock=True)
    if role is None or not role.can_be_assignee:
        raise ValidationFailed("Исполнитель должен быть разработчиком или администратором")
    return target_user_id


async def time_to_take_seconds(session: AsyncSession, ticket_id: int) -> float | None:
    """Seconds from the last entry into `new` to the latest take, read from the journal."""
    take = await session.scalar(
        select(TicketEvent)
        .where(TicketEvent.ticket_id == ticket_id, TicketEvent.event_type == "taken")
        .order_by(TicketEvent.id.desc())
        .limit(1)
    )
    if take is None:
        return None
    entered_new = await session.scalar(
        select(TicketEvent)
        .where(
            TicketEvent.ticket_id == ticket_id,
            TicketEvent.to_status == Status.NEW.value,
            TicketEvent.id < take.id,
        )
        .order_by(TicketEvent.id.desc())
        .limit(1)
    )
    if entered_new is None or take.created_at is None or entered_new.created_at is None:
        return None
    return (take.created_at - entered_new.created_at).total_seconds()
