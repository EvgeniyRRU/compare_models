from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app import metrics
from app.api.auth import AdminUser, Ctx, CurrentUser
from app.bot.context import AppContext
from app.bot.handlers import sync_post_safe
from app.db.models import Ticket, User
from app.domain.state_machine import (
    Action,
    AlreadyTaken,
    NotFound,
    PermissionDenied,
    Priority,
    Role,
    StaleTicket,
    Status,
    TicketError,
)
from app.services.users import RoleChangeBlocked, list_users, set_role

router = APIRouter(prefix="/api")


class UserOut(BaseModel):
    id: int
    telegram_id: int
    username: str | None
    name: str
    role: str | None


class TicketOut(BaseModel):
    id: int
    description: str
    priority: str
    status: str
    request_id: str
    tenant_id: str | None
    version: int
    author: UserOut
    assignee: UserOut | None
    created_at: datetime
    taken_at: datetime | None
    closed_at: datetime | None


class TicketPage(BaseModel):
    items: list[TicketOut]
    total: int
    page: int
    page_size: int


class VersionIn(BaseModel):
    version: int


class ReassignIn(VersionIn):
    assignee_id: int


class RoleIn(BaseModel):
    role: Role | None


def user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id, telegram_id=u.telegram_id, username=u.username, name=u.display_name, role=u.role
    )


def ticket_out(t: Ticket, author: User, assignee: User | None) -> TicketOut:
    return TicketOut(
        id=t.id,
        description=t.description,
        priority=t.priority,
        status=t.status,
        request_id=t.request_id,
        tenant_id=t.tenant_id,
        version=t.version,
        author=user_out(author),
        assignee=user_out(assignee) if assignee else None,
        created_at=t.created_at,
        taken_at=t.taken_at,
        closed_at=t.closed_at,
    )


def http_error(e: TicketError) -> HTTPException:
    if isinstance(e, NotFound):
        return HTTPException(404, str(e))
    if isinstance(e, PermissionDenied):
        return HTTPException(403, str(e))
    if isinstance(e, StaleTicket | AlreadyTaken):
        return HTTPException(409, str(e))
    return HTTPException(422, str(e))


@router.get("/me")
async def me(user: CurrentUser) -> UserOut:
    return user_out(user)


async def _load(ctx: AppContext, ticket_id: int) -> TicketOut:
    author, assignee = aliased(User), aliased(User)
    async with ctx.sm() as s:
        row = (
            await s.execute(
                select(Ticket, author, assignee)
                .join(author, author.id == Ticket.author_id)
                .outerjoin(assignee, assignee.id == Ticket.assignee_id)
                .where(Ticket.id == ticket_id)
            )
        ).one_or_none()
    if row is None:
        raise HTTPException(404, "Обращение не найдено")
    return ticket_out(*row)


@router.get("/tickets")
async def list_tickets(
    ctx: Ctx,
    user: CurrentUser,
    scope: Literal["created", "assigned", "all"] = "all",
    status: Status | None = None,
    priority: Priority | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TicketPage:
    author, assignee = aliased(User), aliased(User)
    conds = []
    if scope == "created":
        conds.append(Ticket.author_id == user.id)
    elif scope == "assigned":
        conds.append(Ticket.assignee_id == user.id)
    if status:
        conds.append(Ticket.status == status.value)
    if priority:
        conds.append(Ticket.priority == priority.value)
    async with ctx.sm() as s:
        total = (
            await s.execute(select(func.count()).select_from(Ticket).where(*conds))
        ).scalar_one()
        rows = (
            await s.execute(
                select(Ticket, author, assignee)
                .join(author, author.id == Ticket.author_id)
                .outerjoin(assignee, assignee.id == Ticket.assignee_id)
                .where(*conds)
                .order_by(Ticket.created_at.desc(), Ticket.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        ).all()
    return TicketPage(
        items=[ticket_out(*r) for r in rows], total=total, page=page, page_size=page_size
    )


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: int, ctx: Ctx, user: CurrentUser) -> TicketOut:
    return await _load(ctx, ticket_id)


async def _admin_action(
    ctx: AppContext,
    ticket_id: int,
    action: Action,
    admin: User,
    version: int,
    target: int | None = None,
) -> TicketOut:
    try:
        async with ctx.sm() as s, s.begin():
            ticket = await ctx.tickets.apply(s, ticket_id, action, admin.id, version, target)
    except TicketError as e:
        raise http_error(e) from e
    metrics.TICKET_TRANSITIONS.labels(action.value, ticket.status).inc()
    await sync_post_safe(ctx, ticket_id)
    return await _load(ctx, ticket_id)


@router.post("/tickets/{ticket_id}/reopen")
async def reopen(ticket_id: int, body: VersionIn, ctx: Ctx, admin: AdminUser) -> TicketOut:
    return await _admin_action(ctx, ticket_id, Action.REOPEN, admin, body.version)


@router.post("/tickets/{ticket_id}/reassign")
async def reassign(ticket_id: int, body: ReassignIn, ctx: Ctx, admin: AdminUser) -> TicketOut:
    return await _admin_action(
        ctx, ticket_id, Action.REASSIGN, admin, body.version, body.assignee_id
    )


@router.post("/tickets/{ticket_id}/restore-post")
async def restore_post(ticket_id: int, ctx: Ctx, admin: AdminUser) -> TicketOut:
    out = await _load(ctx, ticket_id)  # 404, если нет
    try:
        await ctx.syncer.sync(ticket_id, force_new=True)
    except Exception as e:
        raise HTTPException(502, "Не удалось опубликовать пост") from e
    return out


@router.get("/users")
async def users(ctx: Ctx, admin: AdminUser, assignable: bool = False) -> list[UserOut]:
    async with ctx.sm() as s:
        items = await list_users(s)
    if assignable:
        items = [u for u in items if u.role in (Role.DEVELOPER.value, Role.ADMIN.value)]
    return [user_out(u) for u in items]


@router.put("/users/{user_id}/role")
async def change_role(user_id: int, body: RoleIn, ctx: Ctx, admin: AdminUser) -> UserOut:
    try:
        async with ctx.sm() as s, s.begin():
            target = await set_role(s, admin.id, user_id, body.role)
            out = user_out(target)
    except RoleChangeBlocked as e:
        raise HTTPException(409, {"message": str(e), "ticket_ids": e.ticket_ids}) from e
    except TicketError as e:
        raise http_error(e) from e
    return out
