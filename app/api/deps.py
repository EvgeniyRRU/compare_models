"""Зависимости API: initData → пользователь; доступ по роли из БД (без кеша)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, Request

from app.api.auth import AuthError, validate_init_data
from app.models import User
from app.services.users import upsert_user


async def get_current_user(request: Request) -> User:
    settings = request.app.state.settings
    db = request.app.state.db
    init_data = request.query_params.get("initData", "")
    try:
        payload = validate_init_data(
            init_data, settings.bot_token, settings.initdata_max_age_seconds
        )
    except AuthError:
        raise HTTPException(status_code=401, detail="invalid init data") from None
    try:
        telegram_id = int(payload["id"])
    except KeyError, ValueError, TypeError:
        raise HTTPException(status_code=401, detail="invalid user") from None
    async with db.session_factory() as session, session.begin():
        user = await upsert_user(
            session,
            telegram_id=telegram_id,
            username=payload.get("username"),
            first_name=payload.get("first_name"),
            last_name=payload.get("last_name"),
        )
        await session.refresh(user)
    if user.role is None:
        raise HTTPException(status_code=403, detail="no role")
    return user


def require_role(*roles: str) -> Callable[..., Awaitable[User]]:
    async def dep(user: User = Depends(get_current_user)) -> User:  # noqa: B008
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return dep
