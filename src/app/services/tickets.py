from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, TicketEvent, User
from app.domain.state_machine import (
    Action,
    Actor,
    AlreadyTaken,
    EventType,
    InvalidTransition,
    NotFound,
    PermissionDenied,
    Priority,
    Role,
    StaleTicket,
    Status,
    Transition,
    can_be_assignee,
    plan_transition,
)
from app.services.users import get_actor


@dataclass(frozen=True)
class NewTicket:
    description: str
    priority: Priority
    request_id: str
    tenant_id: str | None = None


class TicketService:
    def __init__(self, reminder_intervals: dict[str, timedelta]):
        self._intervals = reminder_intervals

    def _reminder_at(self, priority: str):  # type: ignore[no-untyped-def]
        return func.now() + self._intervals[priority]

    async def create(self, session: AsyncSession, author_id: int, data: NewTicket) -> Ticket:
        """INSERT обращения + событие. Права: support, admin."""
        actor = await get_actor(session, author_id)
        if actor.role not in (Role.SUPPORT, Role.ADMIN):
            raise PermissionDenied("Создавать обращения могут поддержка и админы")
        ticket = Ticket(
            author_id=author_id,
            description=data.description,
            priority=data.priority.value,
            request_id=data.request_id,
            tenant_id=data.tenant_id,
            status=Status.NEW.value,
            version=1,
            next_reminder_at=self._reminder_at(data.priority.value),
        )
        session.add(ticket)
        await session.flush()
        await _write_event(
            session, ticket.id, EventType.CREATED, author_id, None, Status.NEW, None, None, 1
        )
        await session.refresh(ticket)
        return ticket

    async def get(self, session: AsyncSession, ticket_id: int) -> Ticket:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            raise NotFound("Обращение не найдено")
        return ticket

    async def apply(
        self,
        session: AsyncSession,
        ticket_id: int,
        action: Action,
        actor_id: int,
        expected_version: int,
        target_assignee_id: int | None = None,
    ) -> Ticket:
        """Бизнес-изменение одним условным UPDATE (version + status), с событием в журнале."""
        actor = await get_actor(
            session, actor_id, lock_share=action in (Action.TAKE, Action.REASSIGN)
        )
        if action == Action.REASSIGN:
            if target_assignee_id is None:
                raise InvalidTransition("Не указан новый исполнитель")
            target = await get_actor(session, target_assignee_id, lock_share=True)
            if not can_be_assignee(target.role):
                raise InvalidTransition("Исполнителем может быть только developer или admin")

        row = (
            await session.execute(
                select(Ticket.status, Ticket.assignee_id, Ticket.version, Ticket.priority).where(
                    Ticket.id == ticket_id
                )
            )
        ).one_or_none()
        if row is None:
            raise NotFound("Обращение не найдено")
        status = Status(row.status)

        if action == Action.TAKE and status != Status.NEW:
            raise AlreadyTaken("Уже взято")
        if row.version != expected_version:
            raise StaleTicket("Обращение изменилось")
        tr = plan_transition(status, row.assignee_id, action, actor, target_assignee_id)

        values = self._values(tr, row.priority, actor, target_assignee_id)
        stmt = (
            update(Ticket)
            .where(
                Ticket.id == ticket_id,
                Ticket.version == expected_version,
                Ticket.status == status.value,
            )
            .values(version=Ticket.version + 1, **values)
            .returning(Ticket.version)
        )
        new_version = (await session.execute(stmt)).scalar_one_or_none()
        if new_version is None:
            if action == Action.TAKE:
                raise AlreadyTaken("Уже взято")
            raise StaleTicket("Обращение изменилось")

        new_assignee = (
            await session.execute(select(Ticket.assignee_id).where(Ticket.id == ticket_id))
        ).scalar_one()
        await _write_event(
            session,
            ticket_id,
            tr.event_type,
            actor_id,
            status,
            tr.new_status,
            row.assignee_id,
            new_assignee,
            new_version,
        )
        ticket = await session.get(Ticket, ticket_id, populate_existing=True)
        assert ticket is not None
        return ticket

    def _values(
        self, tr: Transition, priority: str, actor: Actor, target_assignee_id: int | None
    ) -> dict[str, object]:
        values: dict[str, object] = {"status": tr.new_status.value}
        if tr.assignee == "set":
            values["assignee_id"] = target_assignee_id if target_assignee_id else actor.id
        elif tr.assignee == "clear":
            values["assignee_id"] = None
        if tr.taken_at == "set":
            values["taken_at"] = func.now()
        elif tr.taken_at == "clear":
            values["taken_at"] = None
        if tr.closed_at == "set":
            values["closed_at"] = func.now()
        elif tr.closed_at == "clear":
            values["closed_at"] = None
        if tr.reminder == "schedule":
            values["next_reminder_at"] = self._reminder_at(priority)
        elif tr.reminder == "clear":
            values["next_reminder_at"] = None
        return values


async def _write_event(
    session: AsyncSession,
    ticket_id: int,
    event_type: EventType,
    actor_id: int,
    old_status: Status | None,
    new_status: Status,
    old_assignee_id: int | None,
    new_assignee_id: int | None,
    version: int,
) -> None:
    await session.execute(
        insert(TicketEvent).values(
            ticket_id=ticket_id,
            event_type=event_type.value,
            actor_id=actor_id,
            old_status=old_status.value if old_status else None,
            new_status=new_status.value,
            old_assignee_id=old_assignee_id,
            new_assignee_id=new_assignee_id,
            version=version,
        )
    )


async def author_and_assignee(session: AsyncSession, ticket: Ticket) -> tuple[User, User | None]:
    author = await session.get(User, ticket.author_id)
    assert author is not None
    assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
    return author, assignee


async def seconds_in_new(session: AsyncSession, ticket_id: int) -> float | None:
    """Время от последнего входа в `new` (создание/отказ/переоткрытие) до сейчас; из журнала."""
    row = (
        await session.execute(
            select(func.extract("epoch", func.now() - TicketEvent.created_at))
            .where(TicketEvent.ticket_id == ticket_id, TicketEvent.new_status == Status.NEW.value)
            .order_by(TicketEvent.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return float(row) if row is not None else None
