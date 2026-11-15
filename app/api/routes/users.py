"""User endpoints for the Mini App REST API (admin only)."""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.errors import raise_http_for_service_error
from app.api.schemas import SetRoleIn, UserOut
from app.db.models import User
from app.db.session import get_session
from app.services.role_service import RoleChangeBlocked, UserNotFound, assign_role
from app.services.ticket_service import PermissionDenied

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[UserOut])
async def list_users(
    user: Annotated[User, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[UserOut]:
    result = await session.scalars(select(User).order_by(User.id))
    return [UserOut.model_validate(u) for u in result]


@router.put("/{user_id}/role", response_model=UserOut)
async def set_user_role(
    user_id: int,
    body: SetRoleIn,
    user: Annotated[User, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserOut | JSONResponse:
    try:
        target = await assign_role(session, actor=user, target_user_id=user_id, new_role=body.role)
    except RoleChangeBlocked as exc:
        return JSONResponse(
            status_code=409,
            content={"detail": str(exc), "active_ticket_ids": exc.active_ticket_ids},
        )
    except (PermissionDenied, UserNotFound) as exc:
        raise_http_for_service_error(exc)
    await session.commit()
    return UserOut.model_validate(target)
