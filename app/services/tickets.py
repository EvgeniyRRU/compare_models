from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Priority, Role, Ticket, TicketEvent, TicketStatus, User

FINAL = {TicketStatus.RESOLVED, TicketStatus.REJECTED, TicketStatus.DUPLICATE}
ACTIVE = {TicketStatus.IN_PROGRESS, TicketStatus.WAITING_INFO}


def reminder_minutes(priority: Priority) -> int:
    settings = get_settings()
    return {
        Priority.CRITICAL: settings.critical_reminder_minutes,
        Priority.HIGH: settings.high_reminder_minutes,
        Priority.MEDIUM: settings.medium_reminder_minutes,
        Priority.LOW: settings.low_reminder_minutes,
    }[priority]


class TicketError(Exception):
    def __init__(self, message: str, *, conflict: bool = False):
        super().__init__(message)
        self.conflict = conflict


async def create_ticket(
    session: AsyncSession,
    *,
    author: User,
    description: str,
    priority: Priority,
    request_id: str,
    tenant_id: str | None,
) -> Ticket:
    locked_author = await session.scalar(
        select(User).where(User.id == author.id).with_for_update(read=True)
    )
    if locked_author is None:
        raise TicketError("Пользователь не найден")
    author = locked_author
    if author.role not in (Role.SUPPORT, Role.ADMIN):
        raise TicketError("Нет доступа")
    if (
        not description.strip()
        or len(description) > 3000
        or not request_id.strip()
        or len(request_id) > 64
    ):
        raise TicketError("Проверьте описание и ID заявки")
    now = datetime.now(UTC)
    ticket = Ticket(
        author_id=author.id,
        description=description.strip(),
        priority=priority,
        request_id=request_id.strip(),
        tenant_id=tenant_id,
        status=TicketStatus.NEW,
        version=1,
        next_reminder_at=now + timedelta(minutes=reminder_minutes(priority)),
    )
    session.add(ticket)
    await session.flush()
    session.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_id=author.id,
            kind="created",
            old_status=None,
            new_status=ticket.status,
            old_assignee_id=None,
            new_assignee_id=None,
            version=1,
        )
    )
    from app.core.metrics import TICKETS_CREATED

    TICKETS_CREATED.labels(priority.value).inc()
    return ticket


async def transition(
    session: AsyncSession,
    *,
    actor: User,
    ticket_id: int,
    expected_version: int,
    expected_status: TicketStatus,
    action: str,
    assignee_id: int | None = None,
) -> Ticket:
    locked_actor = await session.scalar(
        select(User).where(User.id == actor.id).with_for_update(read=True)
    )
    if locked_actor is None:
        raise TicketError("Пользователь не найден")
    actor = locked_actor
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise TicketError("Обращение не найдено")
    if ticket.version != expected_version or ticket.status != expected_status:
        raise TicketError("Обращение изменилось", conflict=True)
    old_status, old_assignee = ticket.status, ticket.assignee_id
    new_status, new_assignee = old_status, old_assignee
    now = datetime.now(UTC)
    time_to_take_seconds = None
    if action == "take":
        if old_status != TicketStatus.NEW or actor.role not in (Role.DEVELOPER, Role.ADMIN):
            raise TicketError("Уже взято или нет доступа", conflict=old_status != TicketStatus.NEW)
        # Share lock serializes role demotion against taking new work.
        if actor.role not in (Role.DEVELOPER, Role.ADMIN):
            raise TicketError("Нет доступа")
        last_new_event = await session.scalar(
            select(TicketEvent)
            .where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.kind.in_(["created", "released", "reopened"]),
            )
            .order_by(TicketEvent.id.desc())
            .limit(1)
        )
        if last_new_event is not None and last_new_event.created_at is not None:
            time_to_take_seconds = max(0, (now - last_new_event.created_at).total_seconds())
        new_status, new_assignee = TicketStatus.IN_PROGRESS, actor.id
    elif action in ("waiting", "resume", "resolve", "reject", "duplicate", "release"):
        if old_status not in ACTIVE or actor.role != Role.ADMIN and old_assignee != actor.id:
            raise TicketError("Нет доступа")
        if action == "waiting" and old_status == TicketStatus.IN_PROGRESS:
            new_status = TicketStatus.WAITING_INFO
        elif action == "resume" and old_status == TicketStatus.WAITING_INFO:
            new_status = TicketStatus.IN_PROGRESS
        elif action in ("resolve", "reject", "duplicate"):
            new_status = {
                "resolve": TicketStatus.RESOLVED,
                "reject": TicketStatus.REJECTED,
                "duplicate": TicketStatus.DUPLICATE,
            }[action]
        elif action == "release":
            new_status, new_assignee = TicketStatus.NEW, None
        else:
            raise TicketError("Недопустимый переход")
    elif action == "reopen":
        if actor.role != Role.ADMIN or old_status not in FINAL:
            raise TicketError("Только администратор может переоткрыть обращение")
        new_status, new_assignee = TicketStatus.NEW, None
    elif action == "reassign":
        if actor.role != Role.ADMIN or old_status not in ACTIVE or assignee_id is None:
            raise TicketError("Недопустимое переназначение")
        target = await session.scalar(
            select(User).where(User.id == assignee_id).with_for_update(read=True)
        )
        if target is None or target.role not in (Role.DEVELOPER, Role.ADMIN):
            raise TicketError("Исполнитель должен быть разработчиком или администратором")
        new_assignee = target.id
    else:
        raise TicketError("Неизвестное действие")
    closed_at = now if new_status in FINAL else None
    if new_status == TicketStatus.NEW:
        taken_at = None
    elif action in ("take", "reassign"):
        taken_at = now
    else:
        taken_at = ticket.taken_at
    reminder = (
        now + timedelta(minutes=reminder_minutes(ticket.priority))
        if new_status == TicketStatus.NEW
        else None
    )
    result = await session.execute(
        update(Ticket)
        .where(
            Ticket.id == ticket_id,
            Ticket.version == expected_version,
            Ticket.status == expected_status,
        )
        .values(
            status=new_status,
            assignee_id=new_assignee,
            taken_at=taken_at,
            closed_at=closed_at,
            next_reminder_at=reminder,
            version=Ticket.version + 1,
        )
    )
    if getattr(result, "rowcount", 0) != 1:
        raise TicketError("Обращение изменилось", conflict=True)
    if time_to_take_seconds is not None:
        from app.core.metrics import TIME_TO_TAKE

        TIME_TO_TAKE.observe(time_to_take_seconds)
    session.add(
        TicketEvent(
            ticket_id=ticket_id,
            actor_id=actor.id,
            kind={
                "take": "taken",
                "release": "released",
                "reopen": "reopened",
                "reassign": "reassigned",
            }.get(action, "status_changed"),
            old_status=old_status,
            new_status=new_status,
            old_assignee_id=old_assignee,
            new_assignee_id=new_assignee,
            version=expected_version + 1,
        )
    )
    from app.core.metrics import STATUS_TRANSITIONS

    STATUS_TRANSITIONS.labels(old_status.value, new_status.value).inc()
    await session.flush()
    await session.refresh(ticket)
    return ticket
