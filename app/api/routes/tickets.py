"""Ticket endpoints for the Mini App REST API."""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.errors import raise_http_for_service_error
from app.api.schemas import ReassignIn, ReopenIn, RestorePostOut, TicketListOut, TicketOut
from app.db.enums import Priority, Role, TicketStatus
from app.db.models import Ticket, User
from app.db.session import async_session_factory, engine, get_session
from app.services.post_sync import restore_post, sync_ticket
from app.services.render import user_display_name
from app.services.ticket_service import (
    PermissionDenied,
    TicketAlreadyTaken,
    TicketChanged,
    reassign,
    reopen,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tickets", tags=["tickets"])


async def _resync(request: Request, ticket_id: int) -> None:
    """Re-sync a ticket's dev-chat post after a business change.

    Failures are logged, never raised — the background loop re-syncs stale posts.
    A missing client (e.g. tests that don't inject one) skips the sync entirely.
    """
    client = getattr(request.app.state, "post_client", None)
    if client is None:
        return
    try:
        await sync_ticket(
            engine=engine,
            session_factory=async_session_factory,
            ticket_id=ticket_id,
            client=client,
        )
    except Exception:
        logger.exception("post re-sync failed for ticket %d", ticket_id)


async def _user_name_map(session: AsyncSession, user_ids: set[int]) -> dict[int, str]:
    """Batch-fetch the given users into an ``id -> display name`` map."""
    if not user_ids:
        return {}
    result = await session.scalars(select(User).where(User.id.in_(user_ids)))
    return {u.id: user_display_name(u) for u in result}


async def _ticket_out(session: AsyncSession, ticket: Ticket) -> TicketOut:
    """Build a ``TicketOut`` with ``author_name``/``assignee_name`` populated."""
    ids = {ticket.author_id}
    if ticket.assignee_id is not None:
        ids.add(ticket.assignee_id)
    names = await _user_name_map(session, ids)
    out = TicketOut.model_validate(ticket)
    out.author_name = names[ticket.author_id]
    out.assignee_name = names[ticket.assignee_id] if ticket.assignee_id is not None else None
    return out


@router.get("", response_model=TicketListOut)
async def list_tickets(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    scope: Literal["all", "authored", "assigned"] = "authored",
    status: TicketStatus | None = None,
    priority: Priority | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TicketListOut:
    if scope == "all" and user.role is not Role.admin:
        raise HTTPException(status_code=403, detail="Требуются права администратора")

    conditions = []
    if scope == "authored":
        conditions.append(Ticket.author_id == user.id)
    elif scope == "assigned":
        conditions.append(Ticket.assignee_id == user.id)
    if status is not None:
        conditions.append(Ticket.status == status)
    if priority is not None:
        conditions.append(Ticket.priority == priority)

    total = await session.scalar(select(func.count()).select_from(Ticket).where(*conditions))
    result = await session.scalars(
        select(Ticket)
        .where(*conditions)
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )
    tickets = list(result)
    user_ids = {t.author_id for t in tickets} | {
        t.assignee_id for t in tickets if t.assignee_id is not None
    }
    names = await _user_name_map(session, user_ids)
    items: list[TicketOut] = []
    for t in tickets:
        out = TicketOut.model_validate(t)
        out.author_name = names[t.author_id]
        out.assignee_name = names[t.assignee_id] if t.assignee_id is not None else None
        items.append(out)
    return TicketListOut(items=items, total=total or 0, page=page, page_size=page_size)


@router.get("/{ticket_id}", response_model=TicketOut)
async def get_ticket(
    ticket_id: int,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketOut:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    if (
        user.role is not Role.admin
        and ticket.author_id != user.id
        and ticket.assignee_id != user.id
    ):
        # 404 (not 403) to avoid leaking the ticket's existence to an outsider.
        raise HTTPException(status_code=404, detail="Обращение не найдено")
    return await _ticket_out(session, ticket)


@router.post("/{ticket_id}/reopen", response_model=TicketOut)
async def reopen_ticket(
    request: Request,
    ticket_id: int,
    body: ReopenIn,
    user: Annotated[User, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketOut:
    try:
        ticket = await reopen(
            session, actor=user, ticket_id=ticket_id, expected_version=body.version
        )
    except (PermissionDenied, TicketChanged, TicketAlreadyTaken) as exc:
        raise_http_for_service_error(exc)
    await session.commit()
    await _resync(request, ticket_id)
    return await _ticket_out(session, ticket)


@router.post("/{ticket_id}/reassign", response_model=TicketOut)
async def reassign_ticket(
    request: Request,
    ticket_id: int,
    body: ReassignIn,
    user: Annotated[User, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TicketOut:
    new_assignee = await session.get(User, body.assignee_id)
    if new_assignee is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        ticket = await reassign(
            session,
            actor=user,
            ticket_id=ticket_id,
            new_assignee=new_assignee,
            expected_version=body.version,
        )
    except (PermissionDenied, TicketChanged, TicketAlreadyTaken) as exc:
        raise_http_for_service_error(exc)
    await session.commit()
    await _resync(request, ticket_id)
    return await _ticket_out(session, ticket)


@router.post("/{ticket_id}/restore-post", response_model=RestorePostOut)
async def restore_ticket_post(
    request: Request,
    ticket_id: int,
    user: Annotated[User, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RestorePostOut:
    ticket = await session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Обращение не найдено")

    client = getattr(request.app.state, "post_client", None)
    if client is not None:
        await restore_post(
            engine=engine,
            session_factory=async_session_factory,
            ticket_id=ticket_id,
            client=client,
        )
    return RestorePostOut(ok=True)
