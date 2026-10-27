from typing import Annotated

from fastapi import APIRouter, Query, Request
from sqlalchemy import func, select

from support_bot.api.auth import AdminUser
from support_bot.api.schemas import RoleRequest, UserOut, UserPage
from support_bot.db.models import User

router = APIRouter(prefix="/api/users")


@router.get("", response_model=UserPage)
async def list_users(
    request: Request,
    actor: AdminUser,
    eligible: bool = False,
    page: Annotated[int, Query(ge=1, le=100000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> UserPage:
    conditions = [User.role.in_(["developer", "admin"])] if eligible else []
    async with request.app.state.db.sessions() as session:
        total = await session.scalar(select(func.count()).select_from(User).where(*conditions))
        users = (
            await session.scalars(
                select(User)
                .where(*conditions)
                .order_by(User.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    return UserPage(
        items=[UserOut.model_validate(user) for user in users],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.patch("/{user_id}/role", response_model=UserOut)
async def set_role(
    user_id: int,
    change: RoleRequest,
    request: Request,
    actor: AdminUser,
) -> UserOut:
    user = await request.app.state.users.set_role(actor.id, user_id, change.role)
    return UserOut.model_validate(user)
