"""Atomic ticket mutations and audit events."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.db.models import Ticket, TicketEvent, User
from support_bot.domain.errors import BusinessError, Conflict, Forbidden, NotFound
from support_bot.domain.transitions import FINAL, authorize_transition, transition
from support_bot.observability.metrics import metrics
from support_bot.services.users import current_user

PRIORITIES = frozenset({"critical", "high", "medium", "low"})


def validated_text(value: str, limit: int, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BusinessError(f"{label}: от 1 до {limit} символов")
    return value.strip()


class TicketService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings):
        self.sessions = sessions
        self.settings = settings

    async def get(self, ticket_id: int) -> Ticket:
        async with self.sessions() as session:
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise NotFound()
            return ticket

    async def create(
        self,
        actor_id: int,
        description: str,
        priority: str,
        request_id: str,
        tenant_id: str | None = None,
    ) -> Ticket:
        async with self.sessions.begin() as session:
            return await self.create_in(
                session, actor_id, description, priority, request_id, tenant_id
            )

    async def create_in(
        self,
        session: AsyncSession,
        actor_id: int,
        description: str,
        priority: str,
        request_id: str,
        tenant_id: str | None = None,
    ) -> Ticket:
        actor = await current_user(session, actor_id)
        if actor.role not in {"support", "admin"}:
            raise Forbidden()
        description = validated_text(description, 3000, "Описание")
        request_id = validated_text(request_id, 64, "ID заявки")
        if tenant_id is not None:
            tenant_id = validated_text(tenant_id, 64, "ID арендатора")
        if priority not in PRIORITIES:
            raise BusinessError("Неизвестный приоритет")
        now = datetime.now(UTC)
        ticket = Ticket(
            author_id=actor_id,
            description=description,
            priority=priority,
            request_id=request_id,
            tenant_id=tenant_id,
            status="new",
            version=1,
            next_reminder_at=now + timedelta(seconds=self.settings.reminder_seconds(priority)),
        )
        session.add(ticket)
        await session.flush()
        session.add(
            TicketEvent(
                ticket_id=ticket.id,
                actor_id=actor_id,
                event_type="created",
                old_status=None,
                new_status="new",
                old_assignee_id=None,
                new_assignee_id=None,
                version=1,
            )
        )
        await session.flush()
        event.listen(
            session.sync_session,
            "after_commit",
            lambda _: metrics.tickets_created.labels(priority).inc(),
            once=True,
        )
        return ticket

    async def change(
        self,
        actor_id: int,
        ticket_id: int,
        expected_version: int,
        expected_status: str,
        action: str,
        assignee_id: int | None = None,
    ) -> Ticket:
        async with self.sessions.begin() as session:
            # Assignee locks prevent a concurrent role demotion from bypassing its check.
            lock_ids = {actor_id}
            if action == "reassign" and assignee_id is not None:
                lock_ids.add(assignee_id)
            locked_users = {
                user.id: user
                for user in await session.scalars(
                    select(User)
                    .where(User.id.in_(lock_ids))
                    .order_by(User.id)
                    .with_for_update(read=True),
                )
            }
            actor = locked_users.get(actor_id)
            if actor is None:
                raise NotFound("Пользователь не найден")
            destination = None
            if action == "reassign":
                if actor.role != "admin":
                    raise Forbidden()
                if assignee_id is None:
                    raise BusinessError("Выберите исполнителя")
                destination = locked_users.get(assignee_id)
                if destination is None:
                    raise NotFound("Пользователь не найден")
                if destination.role not in {"developer", "admin"}:
                    raise Forbidden("Исполнитель должен быть разработчиком или администратором")
            ticket = await session.get(Ticket, ticket_id)
            if ticket is None:
                raise NotFound()
            authorize_transition(action, actor.role, actor_id, ticket.assignee_id)
            if ticket.version != expected_version or ticket.status != expected_status:
                raise Conflict("Уже взято" if action == "take" else "Обращение изменилось")
            status, event_type = transition(
                ticket.status, action, actor.role, actor_id, ticket.assignee_id
            )
            old_status, old_assignee = ticket.status, ticket.assignee_id
            entered_new = None
            if action == "take":
                entered_new = await session.scalar(
                    select(TicketEvent.created_at)
                    .where(
                        TicketEvent.ticket_id == ticket_id,
                        TicketEvent.new_status == "new",
                    )
                    .order_by(TicketEvent.version.desc())
                    .limit(1)
                )
            now = datetime.now(UTC)
            changes: dict[str, Any] = {"status": status, "version": Ticket.version + 1}
            if action in {"take", "reassign"}:
                new_assignee = destination.id if destination else actor_id
                if action == "reassign" and new_assignee == ticket.assignee_id:
                    raise BusinessError("Пользователь уже назначен исполнителем")
                changes.update(assignee_id=new_assignee, taken_at=now)
            elif status == "new":
                changes.update(assignee_id=None, taken_at=None)
            changes["closed_at"] = now if status in FINAL else None
            changes["next_reminder_at"] = (
                now + timedelta(seconds=self.settings.reminder_seconds(ticket.priority))
                if status == "new"
                else None
            )
            statement = (
                update(Ticket)
                .where(
                    Ticket.id == ticket_id,
                    Ticket.version == expected_version,
                    Ticket.status == expected_status,
                )
                .values(**changes)
                .returning(Ticket)
            )
            changed = (
                await session.scalars(
                    statement.execution_options(
                        synchronize_session=False,
                        populate_existing=True,
                    )
                )
            ).one_or_none()
            if changed is None:
                raise Conflict("Уже взято" if action == "take" else "Обращение изменилось")
            session.add(
                TicketEvent(
                    ticket_id=ticket_id,
                    actor_id=actor_id,
                    event_type=event_type,
                    old_status=old_status,
                    new_status=changed.status,
                    old_assignee_id=old_assignee,
                    new_assignee_id=changed.assignee_id,
                    version=changed.version,
                )
            )
            await session.flush()

            def observe_commit(_: object) -> None:
                if old_status != status:
                    metrics.transitions.labels(old_status, status).inc()
                if entered_new is not None:
                    metrics.time_to_take.observe(max(0, (now - entered_new).total_seconds()))

            event.listen(session.sync_session, "after_commit", observe_commit, once=True)
            return changed
