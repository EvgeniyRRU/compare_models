"""REST API Mini App под /api. Эндпоинты тонкие: вся логика в сервисах."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Query

from support_bot.api.deps import ContainerDep, UserDep
from support_bot.api.schemas import (
    MeOut,
    ReassignIn,
    RoleIn,
    Scope,
    TicketDetailOut,
    TicketOut,
    TicketPageOut,
    TicketPermissions,
    UserOut,
    VersionIn,
)
from support_bot.container import Container
from support_bot.domain.enums import ACTIVE_STATUSES, FINAL_STATUSES, Priority, TicketStatus
from support_bot.domain.errors import NotFoundError, PermissionDeniedError
from support_bot.domain.state_machine import Action, can_be_assignee, can_create, is_admin
from support_bot.services.views import TicketListFilter, TicketView, UserInfo

router = APIRouter(prefix="/api", tags=["mini-app"])


def _permissions(user: UserInfo, view: TicketView) -> TicketPermissions:
    admin = is_admin(user.role)
    return TicketPermissions(
        can_reopen=admin and view.status in FINAL_STATUSES,
        can_reassign=admin and view.status in ACTIVE_STATUSES,
        can_restore_post=admin,
    )


async def _detail(container: Container, user: UserInfo, ticket_id: int) -> TicketDetailOut:
    view = await container.tickets.get_view(ticket_id)
    if view is None:
        raise NotFoundError("Обращение не найдено")
    return TicketDetailOut(**TicketOut.of(view).model_dump(), permissions=_permissions(user, view))


@router.get("/me")
async def me(user: UserDep) -> MeOut:
    return MeOut.of(user)


@router.get("/tickets")
async def list_tickets(
    user: UserDep,
    container: ContainerDep,
    scope: Scope = Scope.ALL,
    status_: Annotated[list[TicketStatus], Query(alias="status")] = [],  # noqa: B006
    priority: Annotated[list[Priority], Query()] = [],  # noqa: B006
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TicketPageOut:
    flt = TicketListFilter(statuses=tuple(status_), priorities=tuple(priority))
    match scope:
        case Scope.CREATED:
            if not can_create(user.role):
                raise PermissionDeniedError()
            flt = TicketListFilter(user.id, None, flt.statuses, flt.priorities)
        case Scope.ASSIGNED:
            if not can_be_assignee(user.role):
                raise PermissionDeniedError()
            flt = TicketListFilter(None, user.id, flt.statuses, flt.priorities)
        case Scope.ALL:
            pass
    return TicketPageOut.of(await container.tickets.list_views(flt, page, page_size))


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: int, user: UserDep, container: ContainerDep) -> TicketDetailOut:
    return await _detail(container, user, ticket_id)


@router.post("/tickets/{ticket_id}/reopen")
async def reopen(
    ticket_id: int, body: VersionIn, user: UserDep, container: ContainerDep
) -> TicketDetailOut:
    with structlog.contextvars.bound_contextvars(ticket_id=ticket_id):
        try:
            await container.tickets.apply_action(user.id, ticket_id, Action.REOPEN, body.version)
        finally:
            # и после успеха, и при конфликте версий пост пересинхронизируется
            await container.post_sync.sync_safely(ticket_id)
    return await _detail(container, user, ticket_id)


@router.post("/tickets/{ticket_id}/reassign")
async def reassign(
    ticket_id: int, body: ReassignIn, user: UserDep, container: ContainerDep
) -> TicketDetailOut:
    with structlog.contextvars.bound_contextvars(ticket_id=ticket_id):
        try:
            await container.tickets.reassign(user.id, ticket_id, body.version, body.assignee_id)
        finally:
            await container.post_sync.sync_safely(ticket_id)
    return await _detail(container, user, ticket_id)


@router.post("/tickets/{ticket_id}/restore-post")
async def restore_post(ticket_id: int, user: UserDep, container: ContainerDep) -> TicketDetailOut:
    with structlog.contextvars.bound_contextvars(ticket_id=ticket_id):
        await container.post_sync.restore(user.id, ticket_id)
    return await _detail(container, user, ticket_id)


@router.get("/users")
async def list_users(user: UserDep, container: ContainerDep) -> list[UserOut]:
    return [UserOut.of(u) for u in await container.users.list_users(user.id)]


@router.get("/users/assignees")
async def list_assignees(user: UserDep, container: ContainerDep) -> list[UserOut]:
    return [UserOut.of(u) for u in await container.users.list_assignees(user.id)]


@router.put("/users/{user_id}/role")
async def set_role(user_id: int, body: RoleIn, user: UserDep, container: ContainerDep) -> UserOut:
    return UserOut.of(await container.users.set_role(user.id, user_id, body.role))
