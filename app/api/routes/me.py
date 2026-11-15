"""Current-user endpoint for the Mini App REST API (role discovery)."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.schemas import UserOut
from app.db.models import User

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=UserOut)
async def get_me(user: Annotated[User, Depends(get_current_user)]) -> UserOut:
    """Return the authenticated user, primarily so the Mini App can discover its role."""
    return UserOut.model_validate(user)
