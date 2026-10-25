"""Бизнес-операции над обращениями.

Каждое бизнес-изменение — один условный UPDATE с проверкой ожидаемых status и version,
увеличением version и записью события в журнал в той же транзакции.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import Interval, extract, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.db.engine import SessionFactory
from support_bot.db.models import Ticket, TicketEvent
from support_bot.domain.enums import (
    NEW_ENTRY_EVENTS,
    Priority,
    TicketEventType,
    TicketStatus,
)
from support_bot.domain.errors import (
    AlreadyTakenError,
    InvalidAssigneeError,
    NotFoundError,
    PermissionDeniedError,
    StaleVersionError,
)
from support_bot.domain.state_machine import (
    Action,
    Changes,
    ReminderOp,
    TimeOp,
    can_be_assignee,
    can_create,
    check_permission,
    check_reassign,
    get_transition,
    plan_transition,
)
from support_bot.domain.validation import TicketData
from support_bot.monitoring import metrics
from support_bot.services.users import load_actor, load_user
from support_bot.services.views import TicketListFilter, TicketPage, TicketView, list_ticket_views
from support_bot.services.views import load_ticket_view as _load_view


@dataclass(frozen=True)
class ActionResult:
    ticket_id: int
    version: int
    old_status: TicketStatus
    new_status: TicketStatus
    action: str


@dataclass(frozen=True)
class _TicketState:
    status: TicketStatus
    version: int
    assignee_id: int | None
    priority: Priority


class TicketService:
    def __init__(
        self, session_factory: SessionFactory, reminder_intervals: Mapping[Priority, timedelta]
    ) -> None:
        self._sf = session_factory
        self._intervals = dict(reminder_intervals)

    def reminder_due(self, priority: Priority) -> Any:
        """SQL-выражение `now() + интервал приоритета` (время БД, общее для всех реплик)."""
        return func.now() + literal(self._intervals[priority], Interval())

    # --- создание --------------------------------------------------------------------------

    async def insert_ticket(self, session: AsyncSession, author_id: int, data: TicketData) -> int:
        """Создать обращение в транзакции вызывающего. Права проверяются здесь же."""
        author = await load_actor(session, author_id)
        if not can_create(author.role):
            raise PermissionDeniedError()
        ticket_id: int = (
            await session.execute(
                insert(Ticket)
                .values(
                    author_id=author_id,
                    description=data.description,
                    priority=data.priority,
                    request_id=data.request_id,
                    tenant_id=data.tenant_id,
                    status=TicketStatus.NEW,
                    next_reminder_at=self.reminder_due(data.priority),
                )
                .returning(Ticket.id)
            )
        ).scalar_one()
        await self._log_event(
            session,
            ticket_id=ticket_id,
            event_type=TicketEventType.CREATED,
            actor_id=author_id,
            old_status=None,
            new_status=TicketStatus.NEW,
            old_assignee_id=None,
            new_assignee_id=None,
            version_after=1,
        )
        return ticket_id

    async def create(self, author_id: int, data: TicketData) -> int:
        async with self._sf.begin() as session:
            ticket_id = await self.insert_ticket(session, author_id, data)
        metrics.TICKETS_CREATED.labels(priority=data.priority.value).inc()
        return ticket_id

    # --- переходы --------------------------------------------------------------------------

    async def apply_action(
        self, actor_id: int, ticket_id: int, action: Action, expected_version: int
    ) -> ActionResult:
        time_to_take: float | None = None
        async with self._sf.begin() as session:
            # Взятие делает актора исполнителем: блокируем его строку FOR SHARE, чтобы
            # параллельная смена роли (FOR UPDATE) не обошла проверку.
            actor = await load_actor(
                session, actor_id, lock="share" if action is Action.TAKE else None
            )
            state = await self._load_state(session, ticket_id)

            if action is Action.TAKE and not can_be_assignee(actor.role):
                raise PermissionDeniedError()
            if state.version != expected_version:
                raise self._stale_error(action, state.status)

            transition = get_transition(action, state.status)
            check_permission(transition, actor.id, actor.role, state.assignee_id)
            changes = plan_transition(transition, actor.id)

            new_version = await self._conditional_update(
                session, ticket_id, state, expected_version, changes, action
            )
            await self._log_event(
                session,
                ticket_id=ticket_id,
                event_type=transition.event_type,
                actor_id=actor.id,
                old_status=state.status,
                new_status=changes.status,
                old_assignee_id=state.assignee_id,
                new_assignee_id=changes.assignee_id
                if changes.assignee_changed
                else state.assignee_id,
                version_after=new_version,
            )
            if action is Action.TAKE:
                time_to_take = await self._seconds_since_new_entry(session, ticket_id)

        metrics.TICKET_TRANSITIONS.labels(
            action=action.value, from_status=state.status.value, to_status=changes.status.value
        ).inc()
        if time_to_take is not None:
            metrics.TIME_TO_TAKE.labels(priority=state.priority.value).observe(time_to_take)
        return ActionResult(ticket_id, new_version, state.status, changes.status, action.value)

    async def reassign(
        self, actor_id: int, ticket_id: int, expected_version: int, new_assignee_id: int
    ) -> ActionResult:
        async with self._sf.begin() as session:
            actor = await load_actor(session, actor_id)
            state = await self._load_state(session, ticket_id)
            if state.version != expected_version:
                raise StaleVersionError()
            # FOR SHARE на новом исполнителе: смена его роли не пройдёт параллельно.
            target = await load_user(session, new_assignee_id, lock="share")
            if target is None:
                raise InvalidAssigneeError("Пользователь не найден")
            changes = check_reassign(
                state.status, actor.role, state.assignee_id, target.id, target.role
            )
            new_version = await self._conditional_update(
                session, ticket_id, state, expected_version, changes, "reassign"
            )
            await self._log_event(
                session,
                ticket_id=ticket_id,
                event_type=TicketEventType.REASSIGNED,
                actor_id=actor.id,
                old_status=state.status,
                new_status=state.status,
                old_assignee_id=state.assignee_id,
                new_assignee_id=target.id,
                version_after=new_version,
            )
        metrics.TICKET_TRANSITIONS.labels(
            action="reassign", from_status=state.status.value, to_status=state.status.value
        ).inc()
        return ActionResult(ticket_id, new_version, state.status, state.status, "reassign")

    # --- чтение ----------------------------------------------------------------------------

    async def get_view(self, ticket_id: int) -> TicketView | None:
        async with self._sf() as session:
            return await _load_view(session, ticket_id)

    async def list_views(self, flt: TicketListFilter, page: int, page_size: int) -> TicketPage:
        async with self._sf() as session:
            return await list_ticket_views(session, flt, page, page_size)

    # --- внутреннее ------------------------------------------------------------------------

    @staticmethod
    def _stale_error(action: Action | str, status: TicketStatus) -> StaleVersionError:
        if action is Action.TAKE and status is not TicketStatus.NEW:
            return AlreadyTakenError()
        return StaleVersionError()

    @staticmethod
    async def _load_state(session: AsyncSession, ticket_id: int) -> _TicketState:
        row = (
            await session.execute(
                select(Ticket.status, Ticket.version, Ticket.assignee_id, Ticket.priority).where(
                    Ticket.id == ticket_id
                )
            )
        ).first()
        if row is None:
            raise NotFoundError("Обращение не найдено")
        return _TicketState(row.status, row.version, row.assignee_id, row.priority)

    async def _conditional_update(
        self,
        session: AsyncSession,
        ticket_id: int,
        state: _TicketState,
        expected_version: int,
        changes: Changes,
        action: Action | str,
    ) -> int:
        values: dict[str, Any] = {"status": changes.status, "version": Ticket.version + 1}
        if changes.assignee_changed:
            values["assignee_id"] = changes.assignee_id
        for column, op in (("taken_at", changes.taken_at), ("closed_at", changes.closed_at)):
            if op is TimeOp.NOW:
                values[column] = func.now()
            elif op is TimeOp.CLEAR:
                values[column] = None
        if changes.reminder is ReminderOp.SCHEDULE:
            values["next_reminder_at"] = self.reminder_due(state.priority)
        elif changes.reminder is ReminderOp.CLEAR:
            values["next_reminder_at"] = None

        new_version = (
            await session.execute(
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.version == expected_version,
                    Ticket.status == state.status,
                )
                .values(**values)
                .returning(Ticket.version)
            )
        ).scalar_one_or_none()
        if new_version is None:
            # Гонка: между чтением и UPDATE обращение изменила другая транзакция.
            current = await self._load_state(session, ticket_id)
            raise self._stale_error(action, current.status)
        return int(new_version)

    @staticmethod
    async def _log_event(
        session: AsyncSession,
        *,
        ticket_id: int,
        event_type: TicketEventType,
        actor_id: int,
        old_status: TicketStatus | None,
        new_status: TicketStatus,
        old_assignee_id: int | None,
        new_assignee_id: int | None,
        version_after: int,
    ) -> None:
        await session.execute(
            insert(TicketEvent).values(
                ticket_id=ticket_id,
                event_type=event_type,
                actor_id=actor_id,
                old_status=old_status,
                new_status=new_status,
                old_assignee_id=old_assignee_id,
                new_assignee_id=new_assignee_id,
                version_after=version_after,
            )
        )

    @staticmethod
    async def _seconds_since_new_entry(session: AsyncSession, ticket_id: int) -> float | None:
        entered = (
            select(TicketEvent.created_at)
            .where(
                TicketEvent.ticket_id == ticket_id,
                TicketEvent.event_type.in_(NEW_ENTRY_EVENTS),
            )
            .order_by(TicketEvent.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        value = await session.scalar(select(extract("epoch", func.now() - entered)))
        return float(value) if value is not None else None
