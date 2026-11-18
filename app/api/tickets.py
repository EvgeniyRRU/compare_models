"""REST API Mini App: обращения."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from app.api.deps import get_current_user, require_role
from app.api.schemas import ReassignRequest, ReopenRequest, TicketOut, TicketsPage
from app.log import get_logger
from app.models import Ticket, User
from app.services import tickets as tickets_svc
from app.services.errors import NotFoundError, PermissionDenied, StaleTicketError
from app.services.post_sync import format_user, sync_ticket_post

log = get_logger("api.tickets")
router = APIRouter()

VALID_STATUS = {"new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate"}
VALID_PRIORITY = {"critical", "high", "medium", "low"}

AuthorAlias = aliased(User)
AssigneeAlias = aliased(User)


def _to_out(t: Ticket, author: User, assignee: User | None) -> TicketOut:
    return TicketOut(
        id=t.id,
        description=t.description,
        priority=t.priority,
        request_id=t.request_id,
        tenant_id=t.tenant_id,
        status=t.status,
        version=t.version,
        author_name=format_user(author),
        assignee_name=format_user(assignee) if assignee else None,
        created_at=t.created_at,
        updated_at=t.updated_at,
        taken_at=t.taken_at,
        closed_at=t.closed_at,
    )


async def _resync(request: Request, ticket_id: int) -> None:
    try:
        await sync_ticket_post(
            request.app.state.settings,
            request.app.state.db,
            request.app.state.tg_app.bot,
            ticket_id,
        )
    except Exception:
        log.exception("post resync failed after api change", ticket_id=ticket_id)


@router.get("/tickets", response_model=TicketsPage)
async def list_tickets(
    request: Request,
    user: User = Depends(get_current_user),
    tab: str = Query("all", pattern="^(mine|all)$"),
    status: str | None = None,
    priority: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
) -> TicketsPage:
    if status is not None and status not in VALID_STATUS:
        raise HTTPException(422, "invalid status")
    if priority is not None and priority not in VALID_PRIORITY:
        raise HTTPException(422, "invalid priority")
    db = request.app.state.db
    conditions = []
    if tab == "mine":
        if user.role == "support":
            conditions.append(Ticket.author_id == user.id)
        elif user.role == "developer":
            conditions.append(Ticket.assignee_id == user.id)
        elif user.role == "admin":
            # admin видит обе вкладки: созданные мной И назначенные мне
            conditions.append(or_(Ticket.author_id == user.id, Ticket.assignee_id == user.id))
    if status:
        conditions.append(Ticket.status == status)
    if priority:
        conditions.append(Ticket.priority == priority)
    async with db.session_factory() as s:
        total = (await s.execute(select(func.count(Ticket.id)).where(*conditions))).scalar_one()
        rows = (
            await s.execute(
                select(Ticket, AuthorAlias, AssigneeAlias)
                .join(AuthorAlias, Ticket.author_id == AuthorAlias.id)
                .outerjoin(AssigneeAlias, Ticket.assignee_id == AssigneeAlias.id)
                .where(*conditions)
                .order_by(Ticket.created_at.desc(), Ticket.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
        ).all()
    return TicketsPage(
        items=[_to_out(t, a, asg) for t, a, asg in rows],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/tickets/{ticket_id}", response_model=TicketOut)
async def get_ticket(
    request: Request, ticket_id: int, user: User = Depends(get_current_user)
) -> TicketOut:
    async with request.app.state.db.session_factory() as s:
        t = await s.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404, "not found")
        author = await s.get(User, t.author_id)
        assert author is not None
        assignee = await s.get(User, t.assignee_id) if t.assignee_id else None
    return _to_out(t, author, assignee)


@router.post("/tickets/{ticket_id}/reopen", response_model=TicketOut)
async def reopen_ticket(
    request: Request,
    ticket_id: int,
    body: ReopenRequest,
    user: User = Depends(require_role("admin")),  # noqa: B008
) -> TicketOut:
    try:
        async with request.app.state.db.session_factory() as s, s.begin():
            t = await tickets_svc.load_ticket(s, ticket_id)
            await tickets_svc.apply_transition(
                s, request.app.state.settings, user.id, t, body.expected_version, "reopen"
            )
            author = await s.get(User, t.author_id)
            assert author is not None
            out = _to_out(t, author, None)
    except NotFoundError:
        raise HTTPException(404, "not found") from None
    except StaleTicketError:
        raise HTTPException(409, "ticket changed") from None
    await _resync(request, ticket_id)
    return out


@router.post("/tickets/{ticket_id}/reassign", response_model=TicketOut)
async def reassign_ticket(
    request: Request,
    ticket_id: int,
    body: ReassignRequest,
    user: User = Depends(require_role("admin")),  # noqa: B008
) -> TicketOut:
    try:
        async with request.app.state.db.session_factory() as s, s.begin():
            t = await tickets_svc.load_ticket(s, ticket_id)
            await tickets_svc.apply_transition(
                s,
                request.app.state.settings,
                user.id,
                t,
                body.expected_version,
                "reassign",
                target_user_id=body.user_id,
            )
            author = await s.get(User, t.author_id)
            assert author is not None
            new_assignee = await s.get(User, t.assignee_id)
            out = _to_out(t, author, new_assignee)
    except NotFoundError:
        raise HTTPException(404, "not found") from None
    except StaleTicketError:
        raise HTTPException(409, "ticket changed") from None
    except PermissionDenied:
        raise HTTPException(403, "assignee must be developer or admin") from None
    await _resync(request, ticket_id)
    return out


@router.post("/tickets/{ticket_id}/restore-post")
async def restore_post(
    request: Request,
    ticket_id: int,
    user: User = Depends(require_role("admin")),  # noqa: B008
) -> dict[str, object]:
    async with request.app.state.db.session_factory() as s:
        t = await s.get(Ticket, ticket_id)
    if t is None:
        raise HTTPException(404, "not found")
    try:
        await sync_ticket_post(
            request.app.state.settings,
            request.app.state.db,
            request.app.state.tg_app.bot,
            ticket_id,
            force_new=True,
        )
    except Exception:
        raise HTTPException(502, "post restore failed") from None
    return {"ok": True, "ticket_id": ticket_id}
