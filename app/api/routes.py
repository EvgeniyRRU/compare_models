from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from structlog.contextvars import bind_contextvars

from app.api.auth import validate_init_data
from app.db.models import Ticket, User
from app.domain.state_machine import InvalidTransition
from app.services.posts import sync_post
from app.services.tickets import Conflict, Forbidden, reassign_ticket, transition_ticket
from app.services.users import ActiveTickets, set_role

router = APIRouter(prefix="/api")


async def current_user(request: Request, authorization: str = Header(default="")):
    try:
        if not authorization.startswith("tma "):
            raise ValueError("missing init data")
        info = validate_init_data(
            authorization[4:],
            request.app.state.settings.bot_token,
            request.app.state.settings.init_data_max_age_seconds,
        )
    except ValueError, KeyError, TypeError:
        raise HTTPException(401, "Неверные данные авторизации")
    async with request.app.state.sessions.begin() as session:
        user = await session.scalar(select(User).where(User.telegram_id == info["id"]))
        if not user or not user.role:
            raise HTTPException(403, "Обратитесь к администратору")
        user.name = (
            " ".join(filter(None, (info.get("first_name"), info.get("last_name"))))[:255]
            or user.name
        )
        user.username = info.get("username")
        bind_contextvars(telegram_user_id=user.telegram_id)
        return user


def ticket_dict(t: Ticket):
    return {
        key: getattr(t, key)
        for key in (
            "id",
            "author_id",
            "assignee_id",
            "description",
            "priority",
            "request_id",
            "tenant_id",
            "status",
            "version",
            "created_at",
            "updated_at",
            "taken_at",
            "closed_at",
        )
    }


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"id": user.id, "name": user.name, "role": user.role}


@router.get("/tickets")
async def tickets(
    request: Request,
    scope: str = "all",
    status: str | None = None,
    priority: str | None = None,
    page: int = 1,
    user: User = Depends(current_user),
):
    if page < 1:
        raise HTTPException(422, "Некорректная страница")
    query = select(Ticket)
    if scope == "created" and user.role in {"support", "admin"}:
        query = query.where(Ticket.author_id == user.id)
    elif scope == "assigned" and user.role in {"developer", "admin"}:
        query = query.where(Ticket.assignee_id == user.id)
    elif scope != "all":
        raise HTTPException(403, "Нет доступа")
    if status:
        query = query.where(Ticket.status == status)
    if priority:
        query = query.where(Ticket.priority == priority)
    async with request.app.state.sessions() as session:
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = (
            await session.scalars(
                query.order_by(Ticket.created_at.desc(), Ticket.id.desc())
                .offset((page - 1) * 20)
                .limit(20)
            )
        ).all()
    return {"items": [ticket_dict(t) for t in rows], "total": total, "page": page}


@router.get("/tickets/{ticket_id}")
async def ticket_detail(ticket_id: int, request: Request, user: User = Depends(current_user)):
    bind_contextvars(ticket_id=ticket_id)
    async with request.app.state.sessions() as session:
        ticket = await session.get(Ticket, ticket_id)
        if not ticket:
            raise HTTPException(404, "Обращение не найдено")
        author = await session.get(User, ticket.author_id)
        assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
        return ticket_dict(ticket) | {
            "author_name": author.name if author else None,
            "assignee_name": assignee.name if assignee else None,
        }


class VersionRequest(BaseModel):
    version: int
    status: str


class AssignRequest(VersionRequest):
    assignee_id: int


async def sync(request: Request, ticket_id: int, force: bool = False):
    try:
        await sync_post(
            request.app.state.engine,
            request.app.state.bot.bot,
            request.app.state.settings.dev_chat_id,
            ticket_id,
            force=force,
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Post synchronization failed")


@router.post("/tickets/{ticket_id}/reopen")
async def reopen(
    ticket_id: int, payload: VersionRequest, request: Request, user: User = Depends(current_user)
):
    bind_contextvars(ticket_id=ticket_id)
    if user.role != "admin":
        raise HTTPException(403, "Нет доступа")
    if payload.status not in {"resolved", "rejected", "duplicate"}:
        raise HTTPException(409, "Обращение изменилось")
    try:
        async with request.app.state.sessions.begin() as session:
            changed = await transition_ticket(
                session,
                ticket_id,
                user.id,
                payload.status,
                payload.version,
                "new",
                request.app.state.intervals,
            )
    except Conflict, InvalidTransition:
        await sync(request, ticket_id)
        raise HTTPException(409, "Обращение изменилось")
    await sync(request, ticket_id)
    return ticket_dict(changed)


@router.post("/tickets/{ticket_id}/assign")
async def assign(
    ticket_id: int, payload: AssignRequest, request: Request, user: User = Depends(current_user)
):
    bind_contextvars(ticket_id=ticket_id)
    try:
        async with request.app.state.sessions.begin() as session:
            changed = await reassign_ticket(
                session, ticket_id, user.id, payload.assignee_id, payload.status, payload.version
            )
    except Conflict:
        await sync(request, ticket_id)
        raise HTTPException(409, "Обращение изменилось")
    except Forbidden:
        raise HTTPException(403, "Нет доступа")
    await sync(request, ticket_id)
    return ticket_dict(changed)


@router.post("/tickets/{ticket_id}/restore")
async def restore(ticket_id: int, request: Request, user: User = Depends(current_user)):
    bind_contextvars(ticket_id=ticket_id)
    if user.role != "admin":
        raise HTTPException(403, "Нет доступа")
    async with request.app.state.sessions() as session:
        if not await session.get(Ticket, ticket_id):
            raise HTTPException(404, "Обращение не найдено")
    try:
        await sync_post(
            request.app.state.engine,
            request.app.state.bot.bot,
            request.app.state.settings.dev_chat_id,
            ticket_id,
            force=True,
        )
    except Exception:
        raise HTTPException(503, "Не удалось восстановить пост")
    return {"ok": True}


@router.get("/users")
async def users(request: Request, eligible: bool = False, user: User = Depends(current_user)):
    if user.role != "admin":
        raise HTTPException(403, "Нет доступа")
    async with request.app.state.sessions() as session:
        query = select(User).order_by(User.name)
        if eligible:
            query = query.where(User.role.in_(["developer", "admin"]))
        items = (await session.scalars(query)).all()
        return [
            {
                "id": u.id,
                "telegram_id": u.telegram_id,
                "name": u.name,
                "username": u.username,
                "role": u.role,
            }
            for u in items
        ]


class RoleRequest(BaseModel):
    role: str | None


@router.put("/users/{user_id}/role")
async def change_role(
    user_id: int, payload: RoleRequest, request: Request, user: User = Depends(current_user)
):
    try:
        async with request.app.state.sessions.begin() as session:
            await set_role(session, user.id, user_id, payload.role)
    except ActiveTickets as exc:
        raise HTTPException(
            409, {"message": "Сначала переназначьте обращения", "tickets": exc.ticket_ids}
        )
    except Forbidden:
        raise HTTPException(403, "Нет доступа")
    except LookupError:
        raise HTTPException(404, "Пользователь не найден")
    except ValueError:
        raise HTTPException(422, "Неизвестная роль")
    return {"ok": True}
