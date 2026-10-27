from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select
from sqlalchemy.sql.elements import ColumnElement
from structlog.contextvars import bind_contextvars, bound_contextvars

from support_bot.api.auth import AdminUser, CurrentUser
from support_bot.api.schemas import (
    ChangeRequest,
    ReassignRequest,
    Scope,
    TicketOut,
    TicketPage,
    UserOut,
)
from support_bot.db.models import Ticket, User
from support_bot.domain.enums import Priority, Status
from support_bot.domain.errors import BusinessError, Conflict

router = APIRouter(prefix="/api")


async def card(request: Request, ticket: Ticket) -> TicketOut:
    result = TicketOut.model_validate(ticket)
    result.author = UserOut.model_validate(await request.app.state.users.get(ticket.author_id))
    if ticket.assignee_id is not None:
        result.assignee = UserOut.model_validate(
            await request.app.state.users.get(ticket.assignee_id)
        )
    return result


async def synchronize(request: Request, ticket_id: int, *, force: bool = False) -> None:
    """Committed changes succeed even when Telegram needs background reconciliation."""
    with bound_contextvars(ticket_id=ticket_id):
        try:
            synced = await request.app.state.posts.sync(ticket_id, force=force)
        except Exception as exc:
            from support_bot.observability.sentry import capture_error

            capture_error(exc, component="api_post_sync")
            if force:
                raise BusinessError(
                    "Не удалось восстановить пост. Повторите попытку.", code="post_restore_failed"
                ) from exc
            return
        if force and not synced:
            raise BusinessError(
                "Пост сейчас занят синхронизацией. Повторите попытку.", code="post_restore_failed"
            )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/tickets", response_model=TicketPage)
async def list_tickets(
    request: Request,
    user: CurrentUser,
    scope: Scope = "all",
    status: Status | None = None,
    priority: Priority | None = None,
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> TicketPage:
    conditions: list[ColumnElement[bool]] = []
    if scope == "created":
        conditions.append(Ticket.author_id == user.id)
    elif scope == "assigned":
        conditions.append(Ticket.assignee_id == user.id)
    if status is not None:
        conditions.append(Ticket.status == status)
    if priority is not None:
        conditions.append(Ticket.priority == priority)
    async with request.app.state.db.sessions() as session:
        total = await session.scalar(select(func.count()).select_from(Ticket).where(*conditions))
        tickets = list(
            (
                await session.scalars(
                    select(Ticket)
                    .where(*conditions)
                    .order_by(Ticket.created_at.desc(), Ticket.id.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        )
        ids = {ticket.author_id for ticket in tickets} | {
            ticket.assignee_id for ticket in tickets if ticket.assignee_id is not None
        }
        people = {
            person.id: UserOut.model_validate(person)
            for person in (await session.scalars(select(User).where(User.id.in_(ids)))).all()
        }
    items = []
    for ticket in tickets:
        item = TicketOut.model_validate(ticket)
        item.author = people.get(ticket.author_id)
        item.assignee = people.get(ticket.assignee_id) if ticket.assignee_id else None
        items.append(item)
    return TicketPage(items=items, total=total or 0, page=page, page_size=page_size)


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
async def get_ticket(ticket_id: int, request: Request, user: CurrentUser) -> TicketOut:
    bind_contextvars(ticket_id=ticket_id)
    return await card(request, await request.app.state.tickets.get(ticket_id))


async def mutate(
    request: Request,
    user: User,
    ticket_id: int,
    change: ChangeRequest,
    action: str,
    assignee_id: int | None = None,
) -> TicketOut:
    bind_contextvars(ticket_id=ticket_id)
    try:
        ticket = await request.app.state.tickets.change(
            actor_id=user.id,
            ticket_id=ticket_id,
            expected_version=change.expected_version,
            expected_status=change.expected_status,
            action=action,
            assignee_id=assignee_id,
        )
    except Conflict:
        await synchronize(request, ticket_id)
        raise
    await synchronize(request, ticket_id)
    return await card(request, ticket)


@router.post("/tickets/{ticket_id}/reopen", response_model=TicketOut)
async def reopen(
    ticket_id: int,
    change: ChangeRequest,
    request: Request,
    user: AdminUser,
) -> TicketOut:
    return await mutate(request, user, ticket_id, change, "reopen")


@router.post("/tickets/{ticket_id}/reassign", response_model=TicketOut)
async def reassign(
    ticket_id: int,
    change: ReassignRequest,
    request: Request,
    user: AdminUser,
) -> TicketOut:
    return await mutate(request, user, ticket_id, change, "reassign", change.assignee_id)


@router.post("/tickets/{ticket_id}/restore-post", response_model=TicketOut)
async def restore(ticket_id: int, request: Request, user: AdminUser) -> TicketOut:
    bind_contextvars(ticket_id=ticket_id)
    await request.app.state.tickets.get(ticket_id)
    await synchronize(request, ticket_id, force=True)
    return await card(request, await request.app.state.tickets.get(ticket_id))
