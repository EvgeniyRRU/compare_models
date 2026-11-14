from __future__ import annotations

from fastapi import APIRouter, Depends

from support_bot.api.auth import get_current_user
from support_bot.api.schemas import MeOut, UserOut
from support_bot.db.enums import UserRole
from support_bot.db.models.user import User

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me", response_model=MeOut)
async def get_me(user: User = Depends(get_current_user)) -> MeOut:
    return MeOut(
        user=UserOut.model_validate(user),
        is_admin=user.role == UserRole.ADMIN,
        can_create=user.role in (UserRole.SUPPORT, UserRole.ADMIN),
        can_take=user.role in (UserRole.DEVELOPER, UserRole.ADMIN),
    )
