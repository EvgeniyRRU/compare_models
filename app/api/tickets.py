"""REST API for the Mini App: ticket list/card + admin actions.

Handlers stay thin: queries/filters here, all business rules live in
app.services (permissions, version guard, transitions). Every request re-auths
via initData and re-reads the role from DB.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.auth import AuthUser, get_current_user
from app.config import Settings
from app.db.models import Priority, Role, Ticket, TicketStatus, User
from app.domain.errors import Forbidden, NotFound, ValidationError
from app.domain.state_machine import Action
from app.logging_setup import get_logger
from app.services import tickets as tickets_svc
from app.services.post_sync import sync_ticket

log = get_logger("api.tickets")

router = APIRouter(prefix="/api", tags=["tickets"])

MAX_PAGE = 100
DEFAULT_PAGE = 20

_SCOPES = ("all", "mine", "mine_created", "mine_assigned")


class ExpectedVersion(BaseModel):
    expected_version: int


class ReassignBody(BaseModel):
    expected_version: int
    target_telegram_id: int


def _user_ref(user: User | None) -> dict[str, Any] | None:
    if user is None:
        return None
    return {
        "telegram_id": user.telegram_id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role.value if user.role else None,
    }


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _card(ticket: Ticket, author: User | None, assignee: User | None) -> dict[str, Any]:
    return {
        "id": ticket.id,
        "status": ticket.status.value,
        "priority": ticket.priority.value,
        "request_id": ticket.request_id,
        "tenant_id": ticket.tenant_id,
        "description": ticket.description,
        "author": _user_ref(author),
        "assignee": _user_ref(assignee),
        "version": ticket.version,
        "message_id": ticket.message_id,
        "created_at": _dt(ticket.created_at),
        "taken_at": _dt(ticket.taken_at),
        "closed_at": _dt(ticket.closed_at),
        "next_reminder_at": _dt(ticket.next_reminder_at),
    }


async def _fetch_card(session: AsyncSession, ticket_id: int) -> dict[str, Any]:
    author = aliased(User)
    assignee = aliased(User)
    row = (
        await session.execute(
            select(Ticket, author, assignee)
            .outerjoin(author, Ticket.author_id == author.telegram_id)
            .outerjoin(assignee, Ticket.assignee_id == assignee.telegram_id)
            .where(Ticket.id == ticket_id)
        )
    ).first()
    if row is None:
        raise NotFound()
    ticket, author_u, assignee_u = row
    return _card(ticket, author_u, assignee_u)


def _parse_status(raw: str) -> TicketStatus:
    try:
        return TicketStatus(raw)
    except ValueError:
        raise ValidationError(f"Неизвестный статус: {raw}") from None


def _parse_priority(raw: str) -> Priority:
    try:
        return Priority(raw)
    except ValueError:
        raise ValidationError(f"Неизвестный приоритет: {raw}") from None


@router.get("/tickets")
async def list_tickets(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    scope: str = "all",
    status: str | None = None,
    priority: str | None = None,
    limit: int = DEFAULT_PAGE,
    offset: int = 0,
) -> dict[str, Any]:
    if scope not in _SCOPES:
        raise ValidationError(f"Неизвестная область: {scope}")
    conditions: list[ColumnElement[bool]] = []
    if status is not None:
        conditions.append(Ticket.status == _parse_status(status))
    if priority is not None:
        conditions.append(Ticket.priority == _parse_priority(priority))
    if scope != "all":
        conditions.extend(_scope_conditions(scope, user))

    page_limit = max(1, min(limit, MAX_PAGE))
    page_offset = max(0, offset)

    async with request.app.state.session_factory() as session:
        total = await session.scalar(select(func.count(Ticket.id)).where(*conditions))
        author = aliased(User)
        assignee = aliased(User)
        rows = (
            (
                await session.execute(
                    select(Ticket, author, assignee)
                    .outerjoin(author, Ticket.author_id == author.telegram_id)
                    .outerjoin(assignee, Ticket.assignee_id == assignee.telegram_id)
                    .where(*conditions)
                    .order_by(Ticket.created_at.desc(), Ticket.id.desc())
                    .limit(page_limit)
                    .offset(page_offset)
                )
            )
            .all()
        )
        items = [_card(t, a, g) for t, a, g in rows]
    return {"items": items, "total": total or 0, "limit": page_limit, "offset": page_offset}


def _scope_conditions(scope: str, user: AuthUser) -> list[ColumnElement[bool]]:
    if scope == "mine_created":
        return [Ticket.author_id == user.telegram_id]
    if scope == "mine_assigned":
        return [Ticket.assignee_id == user.telegram_id]
    # "mine": per-role default per spec; admin gets the union of both.
    if user.role is Role.support:
        return [Ticket.author_id == user.telegram_id]
    if user.role is Role.developer:
        return [Ticket.assignee_id == user.telegram_id]
    return [
        or_(Ticket.author_id == user.telegram_id, Ticket.assignee_id == user.telegram_id)
    ]


@router.get("/tickets/{ticket_id}")
async def get_ticket_card(
    ticket_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    async with request.app.state.session_factory() as session:
        return await _fetch_card(session, ticket_id)


async def _run_action(
    request: Request,
    user: AuthUser,
    ticket_id: int,
    action: Action,
    expected_version: int,
    target_assignee_id: int | None = None,
) -> dict[str, Any]:
    cfg: Settings = request.app.state.settings
    async with request.app.state.session_factory() as session, session.begin():
        await tickets_svc.apply_action(
            session,
            cfg,
            ticket_id=ticket_id,
            actor_id=user.telegram_id,
            action=action,
            expected_version=expected_version,
            target_assignee_id=target_assignee_id,
        )
    synced = await _sync_after_change(request, ticket_id)
    async with request.app.state.session_factory() as session:
        card = await _fetch_card(session, ticket_id)
    card["post_synced"] = synced
    return card


async def _sync_after_change(request: Request, ticket_id: int, *, forced: bool = False) -> bool:
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is None:
        return False
    try:
        return await sync_ticket(
            gateway,
            request.app.state.session_factory,
            request.app.state.settings,
            ticket_id,
            forced=forced,
        )
    except Exception:  # pragma: no cover - sync_ticket handles its own failures
        log.exception("api_post_sync_failed", ticket_id=ticket_id)
        return False


@router.post("/tickets/{ticket_id}/reopen")
async def reopen_ticket(
    ticket_id: int,
    body: ExpectedVersion,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return await _run_action(request, user, ticket_id, Action.reopen, body.expected_version)


@router.post("/tickets/{ticket_id}/reassign")
async def reassign_ticket(
    ticket_id: int,
    body: ReassignBody,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    return await _run_action(
        request,
        user,
        ticket_id,
        Action.reassign,
        body.expected_version,
        target_assignee_id=body.target_telegram_id,
    )


@router.post("/tickets/{ticket_id}/restore_post")
async def restore_ticket_post(
    ticket_id: int,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    # Admin-only; version untouched by design (forced publish replaces message_id).
    if user.role is not Role.admin:
        raise Forbidden("Пост восстанавливает только администратор")
    async with request.app.state.session_factory() as session:
        if await session.get(Ticket, ticket_id) is None:
            raise NotFound()
    synced = await _sync_after_change(request, ticket_id, forced=True)
    async with request.app.state.session_factory() as session:
        card = await _fetch_card(session, ticket_id)
    card["post_synced"] = synced
    return card
