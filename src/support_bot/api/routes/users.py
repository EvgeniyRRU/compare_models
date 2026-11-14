from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.api.auth import require_admin
from support_bot.api.schemas import RoleUpdateIn, UserListOut, UserOut
from support_bot.db.models.user import User
from support_bot.db.session import get_session
from support_bot.domain.errors import PermissionDenied, RoleChangeConflict, UserNotFound
from support_bot.services import users as users_service

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=UserListOut)
async def list_users(
    query: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserListOut:
    del admin
    users, total = await users_service.list_users(
        session, query=query, page=page, page_size=page_size
    )
    return UserListOut(
        items=[UserOut.model_validate(user) for user in users],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/assignable", response_model=list[UserOut])
async def list_assignable(
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[UserOut]:
    del admin
    users = await users_service.list_assignable(session)
    return [UserOut.model_validate(user) for user in users]


@router.patch("/{user_id}/role", response_model=UserOut)
async def change_role(
    user_id: int,
    body: RoleUpdateIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserOut:
    try:
        updated = await users_service.set_role(
            session,
            actor_id=admin.id,
            target_id=user_id,
            new_role=body.role,
        )
    except UserNotFound as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail="admin role required") from exc
    except RoleChangeConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "active_tickets", "ticket_ids": exc.ticket_ids},
        ) from exc
    return UserOut.model_validate(updated)
