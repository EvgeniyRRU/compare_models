"""Read-модели: снимки данных для рендера поста, API и бота."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from support_bot.db.models import Ticket, User
from support_bot.domain.enums import Priority, Role, TicketStatus


@dataclass(frozen=True)
class UserInfo:
    id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    role: Role | None

    @property
    def display_name(self) -> str:
        full = " ".join(p for p in (self.first_name, self.last_name) if p).strip()
        if full and self.username:
            return f"{full} (@{self.username})"
        if self.username:
            return f"@{self.username}"
        return full or f"id {self.telegram_id}"

    @classmethod
    def from_model(cls, user: User) -> UserInfo:
        return cls(
            id=user.id,
            telegram_id=user.telegram_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            role=user.role,
        )


@dataclass(frozen=True)
class TicketView:
    id: int
    status: TicketStatus
    priority: Priority
    description: str
    request_id: str
    tenant_id: str | None
    author: UserInfo
    assignee: UserInfo | None
    version: int
    created_at: datetime
    updated_at: datetime
    taken_at: datetime | None
    closed_at: datetime | None
    next_reminder_at: datetime | None
    message_id: int | None
    post_synced_version: int


@dataclass(frozen=True)
class TicketListFilter:
    author_id: int | None = None
    assignee_id: int | None = None
    statuses: tuple[TicketStatus, ...] = ()
    priorities: tuple[Priority, ...] = ()


@dataclass(frozen=True)
class TicketPage:
    items: list[TicketView]
    total: int
    page: int
    page_size: int


_Author = aliased(User, name="author")
_Assignee = aliased(User, name="assignee")


def _base_query() -> Select[Ticket, User, User]:
    return (
        select(Ticket, _Author, _Assignee)
        .join(_Author, _Author.id == Ticket.author_id)
        .outerjoin(_Assignee, _Assignee.id == Ticket.assignee_id)
    )


def _to_view(ticket: Ticket, author: User, assignee: User | None) -> TicketView:
    return TicketView(
        id=ticket.id,
        status=ticket.status,
        priority=ticket.priority,
        description=ticket.description,
        request_id=ticket.request_id,
        tenant_id=ticket.tenant_id,
        author=UserInfo.from_model(author),
        assignee=UserInfo.from_model(assignee) if assignee is not None else None,
        version=ticket.version,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        taken_at=ticket.taken_at,
        closed_at=ticket.closed_at,
        next_reminder_at=ticket.next_reminder_at,
        message_id=ticket.message_id,
        post_synced_version=ticket.post_synced_version,
    )


async def load_ticket_view(session: AsyncSession, ticket_id: int) -> TicketView | None:
    # populate_existing: в длинной сессии не берём устаревший объект из identity map.
    stmt = _base_query().where(Ticket.id == ticket_id).execution_options(populate_existing=True)
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return _to_view(*row)


async def list_ticket_views(
    session: AsyncSession, flt: TicketListFilter, page: int, page_size: int
) -> TicketPage:
    conditions = []
    if flt.author_id is not None:
        conditions.append(Ticket.author_id == flt.author_id)
    if flt.assignee_id is not None:
        conditions.append(Ticket.assignee_id == flt.assignee_id)
    if flt.statuses:
        conditions.append(Ticket.status.in_(flt.statuses))
    if flt.priorities:
        conditions.append(Ticket.priority.in_(flt.priorities))

    total = await session.scalar(select(func.count()).select_from(Ticket).where(*conditions))
    stmt = (
        _base_query()
        .where(*conditions)
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    rows = (await session.execute(stmt)).all()
    return TicketPage(
        items=[_to_view(*row) for row in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
    )
