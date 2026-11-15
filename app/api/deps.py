"""FastAPI dependencies: initData auth, current-user lookup, and admin gate."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import InitData, InvalidInitData, validate_init_data
from app.config import settings
from app.db.enums import Role
from app.db.models import User
from app.db.session import get_session


def get_init_data(
    init_data: Annotated[str | None, Header(alias="X-Telegram-Init-Data")] = None,
) -> InitData:
    """Validate the ``X-Telegram-Init-Data`` header into an :class:`InitData`."""
    if init_data is None:
        raise HTTPException(status_code=401, detail="Невалидная авторизация")
    try:
        return validate_init_data(
            init_data,
            bot_token=settings.bot_token,
            max_age=settings.init_data_max_age,
        )
    except InvalidInitData as exc:
        raise HTTPException(status_code=401, detail="Невалидная авторизация") from exc


async def get_current_user(
    init_data: Annotated[InitData, Depends(get_init_data)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Resolve the authenticated Telegram user from the database."""
    user = await session.scalar(select(User).where(User.telegram_id == init_data.user_id))
    if user is None:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    """Require the authenticated user to be an administrator."""
    if user.role is not Role.admin:
        raise HTTPException(status_code=403, detail="Требуются права администратора")
    return user
