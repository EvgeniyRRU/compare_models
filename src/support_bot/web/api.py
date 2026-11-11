from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.db.models import Ticket, User
from support_bot.domain.errors import NotFound
from support_bot.domain.roles import Role
from support_bot.domain.state_machine import Action
from support_bot.runtime import Runtime
from support_bot.services import tickets as ticket_svc
from support_bot.services import users as user_svc
from support_bot.web.auth import InvalidInitData, verify_init_data

router = APIRouter(prefix="/api")


class CurrentUser(BaseModel):
    id: int
    telegram_id: int
    name: str
    role: Role | None


def _runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


async def current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(status_code=401, detail="Нужна авторизация Telegram")
    runtime = _runtime(request)
    try:
        tg_user = verify_init_data(
            authorization.removeprefix("tma "),
            token=runtime.settings.bot_token.get_secret_value(),
            max_age_seconds=runtime.settings.init_data_max_age_seconds,
        )
    except InvalidInitData as exc:
        raise HTTPException(status_code=401, detail="Неверные данные авторизации") from exc

    async with runtime.sessions.begin() as session:
        user = await user_svc.register_user(
            session,
            settings=runtime.settings,
            telegram_id=int(tg_user["id"]),
            username=tg_user.get("username"),
            full_name=" ".join(
                part for part in (tg_user.get("first_name"), tg_user.get("last_name")) if part
            )
            or str(tg_user["id"]),
        )
        role = await user_svc.current_role(session, user.id)
        return CurrentUser(id=user.id, telegram_id=user.telegram_id, name=user.full_name, role=role)


CurrentDep = Annotated[CurrentUser, Depends(current_user)]


def _require_role(user: CurrentUser) -> Role:
    if user.role is None:
        raise HTTPException(status_code=403, detail="Доступа пока нет. Обратитесь к администратору")
    return user.role


def _require_admin(user: CurrentUser) -> None:
    if _require_role(user) is not Role.ADMIN:
        raise HTTPException(status_code=403, detail="Только для администратора")


class TicketOut(BaseModel):
    id: int
    status: str
    priority: str
    description: str
    request_id: str
    tenant_id: str | None
    author: str | None
    assignee: str | None
    assignee_id: int | None
    version: int
    created_at: datetime
    taken_at: datetime | None
    closed_at: datetime | None


class TicketPage(BaseModel):
    items: list[TicketOut]
    page: int
    size: int
    total: int


class TicketCard(TicketOut):
    can_reopen: bool
    can_reassign: bool
    can_restore_post: bool


class VersionedBody(BaseModel):
    expected_version: int = Field(ge=1)


class ReassignBody(VersionedBody):
    assignee_id: int


class RoleBody(BaseModel):
    role: Role | None


class UserOut(BaseModel):
    id: int
    telegram_id: int
    name: str
    username: str | None
    role: Role | None


def _ticket_out(ticket: Ticket, author: User | None, assignee: User | None) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "status": ticket.status,
        "priority": ticket.priority,
        "description": ticket.description,
        "request_id": ticket.request_id,
        "tenant_id": ticket.tenant_id,
        "author": author.full_name if author else None,
        "assignee": assignee.full_name if assignee else None,
        "assignee_id": ticket.assignee_id,
        "version": ticket.version,
        "created_at": ticket.created_at,
        "taken_at": ticket.taken_at,
        "closed_at": ticket.closed_at,
    }


async def _load(session: AsyncSession, ticket_id: int) -> tuple[Ticket, User | None, User | None]:
    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    if ticket is None:
        raise NotFound("Обращение не найдено")
    author = await session.get(User, ticket.author_id)
    assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
    return ticket, author, assignee


@router.get("/me")
async def me(user: CurrentDep) -> CurrentUser:
    return user


@router.get("/tickets", response_model=TicketPage)
async def list_tickets(
    request: Request,
    user: CurrentDep,
    scope: Annotated[str, Query(pattern="^(created|assigned|all)$")] = "all",
    status: str | None = None,
    priority: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> TicketPage:
    _require_role(user)
    runtime = _runtime(request)
    conditions = []
    if scope == "created":
        conditions.append(Ticket.author_id == user.id)
    elif scope == "assigned":
        conditions.append(Ticket.assignee_id == user.id)
    if status:
        conditions.append(Ticket.status == status)
    if priority:
        conditions.append(Ticket.priority == priority)

    async with runtime.sessions() as session:
        total = await session.scalar(select(func.count()).select_from(Ticket).where(*conditions))
        rows = await session.execute(
            select(Ticket)
            .where(*conditions)
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        items = []
        for ticket in rows.scalars():
            author = await session.get(User, ticket.author_id)
            assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
            items.append(TicketOut(**_ticket_out(ticket, author, assignee)))
    return TicketPage(items=items, page=page, size=size, total=int(total or 0))


@router.get("/tickets/{ticket_id}", response_model=TicketCard)
async def get_ticket(request: Request, user: CurrentDep, ticket_id: int) -> TicketCard:
    _require_role(user)
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        ticket, author, assignee = await _load(session, ticket_id)
    is_admin = user.role is Role.ADMIN
    return TicketCard(
        **_ticket_out(ticket, author, assignee),
        can_reopen=is_admin and ticket.status in ("resolved", "rejected", "duplicate"),
        can_reassign=is_admin and ticket.status in ("in_progress", "waiting_info"),
        can_restore_post=is_admin,
    )


async def _admin_transition(
    request: Request,
    user: CurrentUser,
    ticket_id: int,
    action: Action,
    expected_version: int,
    target_user_id: int | None = None,
) -> TicketOut:
    _require_admin(user)
    runtime = _runtime(request)
    async with runtime.sessions.begin() as session:
        await ticket_svc.transition_at_version(
            session,
            settings=runtime.settings,
            actor_id=user.id,
            ticket_id=ticket_id,
            action=action,
            expected_version=expected_version,
            target_user_id=target_user_id,
            now=datetime.now(UTC),
        )
    await runtime.sync_post(ticket_id)
    async with runtime.sessions() as session:
        ticket, author, assignee = await _load(session, ticket_id)
    return TicketOut(**_ticket_out(ticket, author, assignee))


@router.post("/tickets/{ticket_id}/reopen", response_model=TicketOut)
async def reopen(
    request: Request, user: CurrentDep, ticket_id: int, body: VersionedBody
) -> TicketOut:
    return await _admin_transition(request, user, ticket_id, Action.REOPEN, body.expected_version)


@router.post("/tickets/{ticket_id}/reassign", response_model=TicketOut)
async def reassign(
    request: Request, user: CurrentDep, ticket_id: int, body: ReassignBody
) -> TicketOut:
    return await _admin_transition(
        request, user, ticket_id, Action.REASSIGN, body.expected_version, body.assignee_id
    )


@router.post("/tickets/{ticket_id}/restore-post", status_code=204)
async def restore_post(request: Request, user: CurrentDep, ticket_id: int) -> None:
    """Publish a new post and replace message_id. Technical: version does not change."""
    _require_admin(user)
    runtime = _runtime(request)
    async with runtime.sessions.begin() as session:
        ticket = await session.get(Ticket, ticket_id, populate_existing=True)
        if ticket is None:
            raise NotFound("Обращение не найдено")
        ticket.message_id = None
    await runtime.sync_post(ticket_id)


@router.get("/users", response_model=list[UserOut])
async def list_users(request: Request, user: CurrentDep) -> list[UserOut]:
    _require_admin(user)
    runtime = _runtime(request)
    async with runtime.sessions() as session:
        rows = (await session.scalars(select(User).order_by(User.id))).all()
    return [
        UserOut(
            id=row.id,
            telegram_id=row.telegram_id,
            name=row.full_name,
            username=row.username,
            role=Role(row.role) if row.role else None,
        )
        for row in rows
    ]


@router.put("/users/{telegram_id}/role", response_model=UserOut)
async def set_role(request: Request, user: CurrentDep, telegram_id: int, body: RoleBody) -> UserOut:
    _require_admin(user)
    runtime = _runtime(request)
    async with runtime.sessions.begin() as session:
        row = await user_svc.set_role(
            session,
            admin_id=user.id,
            target_telegram_id=telegram_id,
            role=body.role,
        )
    return UserOut(
        id=row.id,
        telegram_id=row.telegram_id,
        name=row.full_name,
        username=row.username,
        role=Role(row.role) if row.role else None,
    )
