"""REST API for the Mini App roles screen (admin only)."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import AuthUser, get_current_user
from app.db.models import Role, User
from app.domain.errors import Forbidden, ValidationError
from app.logging_setup import get_logger
from app.services import roles as roles_svc

log = get_logger("api.users")

router = APIRouter(prefix="/api", tags=["users"])

MAX_LIST = 100


class RoleBody(BaseModel):
    role: str | None


def _require_admin(user: AuthUser) -> None:
    if user.role is not Role.admin:
        raise Forbidden("Управление ролями доступно только администратору")


def _user_dict(u: User) -> dict[str, Any]:
    return {
        "telegram_id": u.telegram_id,
        "username": u.username,
        "full_name": u.full_name,
        "role": u.role.value if u.role else None,
    }


@router.get("/me")
async def me(user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    """Identity + fresh role for tab/action rendering in the Mini App."""
    return {
        "telegram_id": user.telegram_id,
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role.value,
    }


@router.get("/users")
async def list_users(
    request: Request,
    user: AuthUser = Depends(get_current_user),
    q: str | None = None,
    executors_only: bool = False,
) -> dict[str, Any]:
    """Admin user list for role management; executors_only=true -> reassign picker."""
    _require_admin(user)
    async with request.app.state.session_factory() as session:
        if executors_only:
            users = await roles_svc.users_with_executor_role(session, q)
        else:
            users = await _search_all(session, q)
    return {"items": [_user_dict(u) for u in users[:MAX_LIST]], "total": len(users)}


async def _search_all(session: AsyncSession, q: str | None) -> list[User]:
    stmt = select(User).order_by(User.telegram_id).limit(MAX_LIST)
    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(or_(User.username.ilike(pattern), User.full_name.ilike(pattern)))
    return list((await session.scalars(stmt)).all())


@router.put("/users/{telegram_id}/role")
async def set_user_role(
    telegram_id: int,
    body: RoleBody,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    _require_admin(user)
    role: Role | None
    if body.role is None:
        role = None
    else:
        try:
            role = Role(body.role)
        except ValueError:
            raise ValidationError(f"Неизвестная роль: {body.role}") from None
    async with request.app.state.session_factory() as session, session.begin():
        target = await roles_svc.set_role(session, user.telegram_id, telegram_id, role)
    log.info("api_role_set", actor=user.telegram_id, target=telegram_id, role=body.role)
    return _user_dict(target)
