from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_user
from app.db.models import Role, Ticket, TicketStatus, User
from app.db.session import SessionLocal
from app.services.roles import RoleChangeError, change_user_role
from app.services.tickets import TicketError, transition

router = APIRouter(prefix="/api")


class RoleChange(BaseModel):
    role: Role | None


class TicketAction(BaseModel):
    version: int
    status: TicketStatus
    action: str
    assignee_id: int | None = None


@router.get("/me")
async def me(user: User = Depends(current_user)):
    return {"id": user.id, "role": user.role, "name": user.full_name}


async def session_dep():
    async with SessionLocal() as session:
        yield session


@router.get("/tickets")
async def tickets(
    status: TicketStatus | None = None,
    priority: str | None = None,
    mine: bool = False,
    page: int = 1,
    page_size: int = 30,
    session: AsyncSession = Depends(session_dep),
    user: User = Depends(current_user),
):
    query = select(Ticket).order_by(Ticket.created_at.desc())
    if status:
        query = query.where(Ticket.status == status)
    if priority:
        query = query.where(Ticket.priority == priority)
    if mine:
        if user.role == Role.ADMIN:
            query = query.where((Ticket.author_id == user.id) | (Ticket.assignee_id == user.id))
        elif user.role == Role.SUPPORT:
            query = query.where(Ticket.author_id == user.id)
        elif user.role == Role.DEVELOPER:
            query = query.where(Ticket.assignee_id == user.id)
    size = min(max(page_size, 1), 100)
    query = query.offset((max(page, 1) - 1) * size).limit(size)
    return [
        {
            "id": t.id,
            "author_id": t.author_id,
            "assignee_id": t.assignee_id,
            "description": t.description,
            "priority": t.priority,
            "request_id": t.request_id,
            "tenant_id": t.tenant_id,
            "status": t.status,
            "version": t.version,
            "created_at": t.created_at,
        }
        for t in (await session.scalars(query)).all()
    ]


@router.get("/tickets/{ticket_id}")
async def ticket_detail(
    ticket_id: int, session: AsyncSession = Depends(session_dep), user: User = Depends(current_user)
):
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "Обращение не найдено")
    author = await session.get(User, ticket.author_id)
    assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
    return {
        "id": ticket.id,
        "author_id": ticket.author_id,
        "author_name": author.full_name if author else None,
        "assignee_id": ticket.assignee_id,
        "assignee_name": assignee.full_name if assignee else None,
        "description": ticket.description,
        "priority": ticket.priority,
        "request_id": ticket.request_id,
        "tenant_id": ticket.tenant_id,
        "status": ticket.status,
        "version": ticket.version,
        "created_at": ticket.created_at,
        "updated_at": ticket.updated_at,
        "taken_at": ticket.taken_at,
        "closed_at": ticket.closed_at,
        "next_reminder_at": ticket.next_reminder_at,
        "message_id": ticket.message_id,
        "post_synced_version": ticket.post_synced_version,
    }


@router.get("/users")
async def users(session: AsyncSession = Depends(session_dep), user: User = Depends(current_user)):
    if user.role != Role.ADMIN:
        raise HTTPException(403, "Нет доступа")
    return [
        {
            "id": u.id,
            "telegram_id": u.telegram_id,
            "name": u.full_name,
            "username": u.username,
            "role": u.role,
        }
        for u in (await session.scalars(select(User).order_by(User.full_name))).all()
    ]


@router.put("/users/{user_id}/role")
async def change_role(
    user_id: int,
    body: RoleChange,
    session: AsyncSession = Depends(session_dep),
    user: User = Depends(current_user),
):
    try:
        async with session.begin():
            target = await change_user_role(
                session, actor_id=user.id, target_id=user_id, role=body.role
            )
    except RoleChangeError as exc:
        if exc.active_ticket_ids:
            raise HTTPException(
                409, detail={"message": str(exc), "ticket_ids": exc.active_ticket_ids}
            ) from exc
        raise HTTPException(404 if str(exc) == "Пользователь не найден" else 403, str(exc)) from exc
    return {"id": target.id, "role": target.role}


@router.post("/tickets/{ticket_id}/actions")
async def ticket_action(
    ticket_id: int,
    body: TicketAction,
    session: AsyncSession = Depends(session_dep),
    user: User = Depends(current_user),
):
    if user.role != Role.ADMIN or body.action not in ("reopen", "reassign"):
        raise HTTPException(403, "В Mini App доступны только административные действия")
    try:
        async with session.begin():
            ticket = await transition(
                session,
                actor=user,
                ticket_id=ticket_id,
                expected_version=body.version,
                expected_status=body.status,
                action=body.action,
                assignee_id=body.assignee_id,
            )
        from app.bot.posts import sync_ticket
        from app.main import bot_app

        if bot_app:
            await sync_ticket(bot_app.bot, ticket_id)
        return {"id": ticket.id, "status": ticket.status, "version": ticket.version}
    except (TicketError, ValueError) as exc:
        raise HTTPException(
            409 if isinstance(exc, TicketError) and exc.conflict else 400, str(exc)
        ) from exc


@router.post("/tickets/{ticket_id}/restore-post")
async def restore_post(ticket_id: int, user: User = Depends(current_user)):
    if user.role != Role.ADMIN:
        raise HTTPException(403, "Нет доступа")
    from app.bot.posts import sync_ticket
    from app.main import bot_app

    if bot_app is None:
        raise HTTPException(503, "Бот не настроен")
    await sync_ticket(bot_app.bot, ticket_id, force_new=True)
    return {"status": "published"}
