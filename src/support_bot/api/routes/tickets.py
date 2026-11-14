from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from support_bot.api.auth import get_current_user, require_admin
from support_bot.api.schemas import ReassignIn, TicketListOut, TicketOut, VersionActionIn
from support_bot.db.enums import TicketPriority, TicketStatus
from support_bot.db.models.ticket import Ticket
from support_bot.db.models.user import User
from support_bot.db.session import get_session
from support_bot.domain.errors import (
    LockUnavailable,
    PermissionDenied,
    StatusConflict,
    TicketNotFound,
    UserNotFound,
    ValidationError,
    VersionConflict,
)
from support_bot.observability.metrics import observe_error
from support_bot.observability.sentry import capture_exception
from support_bot.services import tickets as tickets_service

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/tickets", tags=["tickets"])


@router.get("", response_model=TicketListOut)
async def list_tickets(
    request: Request,
    scope: Literal["authored", "assigned", "all"] = "authored",
    status_filter: TicketStatus | None = Query(default=None, alias="status"),
    priority: TicketPriority | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TicketListOut:
    conditions = []
    if scope == "authored":
        conditions.append(Ticket.author_id == user.id)
    elif scope == "assigned":
        conditions.append(Ticket.assignee_id == user.id)
    if status_filter is not None:
        conditions.append(Ticket.status == status_filter)
    if priority is not None:
        conditions.append(Ticket.priority == priority)

    total = int(
        await session.scalar(select(func.count()).select_from(Ticket).where(*conditions)) or 0
    )
    statement = (
        select(Ticket)
        .options(joinedload(Ticket.author), joinedload(Ticket.assignee))
        .where(*conditions)
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    tickets = list((await session.scalars(statement)).all())
    return TicketListOut(
        items=[TicketOut.model_validate(ticket) for ticket in tickets],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(
    ticket_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TicketOut:
    del user
    try:
        ticket = await tickets_service.get_ticket_with_relations(session, ticket_id)
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail="ticket not found") from exc
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/reopen", response_model=TicketOut)
async def reopen_ticket(
    ticket_id: int,
    body: VersionActionIn,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> TicketOut:
    try:
        await tickets_service.reopen_ticket(
            session,
            ticket_id=ticket_id,
            expected_version=body.version,
            actor_id=admin.id,
            policy=request.app.state.reminder_policy,
        )
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail="ticket not found") from exc
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail="ticket changed") from exc
    except StatusConflict as exc:
        raise HTTPException(status_code=409, detail="ticket changed") from exc
    await _sync_quietly(request, ticket_id)
    ticket = await tickets_service.get_ticket_with_relations(session, ticket_id)
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/reassign", response_model=TicketOut)
async def reassign_ticket(
    ticket_id: int,
    body: ReassignIn,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> TicketOut:
    try:
        await tickets_service.reassign_ticket(
            session,
            ticket_id=ticket_id,
            expected_version=body.version,
            actor_id=admin.id,
            new_assignee_id=body.assignee_id,
        )
    except TicketNotFound as exc:
        raise HTTPException(status_code=404, detail="ticket not found") from exc
    except (UserNotFound, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except VersionConflict as exc:
        raise HTTPException(status_code=409, detail="ticket changed") from exc
    except (StatusConflict, PermissionDenied) as exc:
        raise HTTPException(status_code=409, detail="ticket changed") from exc
    await _sync_quietly(request, ticket_id)
    ticket = await tickets_service.get_ticket_with_relations(session, ticket_id)
    return TicketOut.model_validate(ticket)


@router.post("/{ticket_id}/restore-post", status_code=status.HTTP_200_OK)
async def restore_post(
    ticket_id: int,
    request: Request,
    admin: User = Depends(require_admin),
) -> dict[str, bool]:
    del admin
    try:
        synced = await request.app.state.post_sync.sync(ticket_id, force_new=True)
    except LockUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="post sync is busy, retry later",
        ) from exc
    if not synced:
        raise HTTPException(status_code=404, detail="ticket not found")
    return {"ok": True}


async def _sync_quietly(request: Request, ticket_id: int) -> None:
    try:
        await request.app.state.post_sync.sync(ticket_id)
    except Exception as exc:
        logger.exception(
            "post sync after API action failed",
            component="api",
            ticket_id=ticket_id,
        )
        observe_error("api")
        capture_exception(exc, component="api", ticket_id=ticket_id)
