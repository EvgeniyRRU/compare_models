from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.db.enums import UserRole
from support_bot.db.models.user import User
from support_bot.db.session import get_session
from support_bot.services import users as users_service
from support_bot.services.users import TelegramProfile


class InitDataError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WebAppUser:
    id: int
    username: str | None
    first_name: str | None
    last_name: str | None


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age_seconds: int,
    now: float | None = None,
) -> WebAppUser:
    if not init_data or not bot_token:
        raise InitDataError("empty init data")

    parsed = parse_qs(init_data, keep_blank_values=True)
    values: dict[str, str] = {}
    for key, items in parsed.items():
        if len(items) != 1:
            raise InitDataError("duplicate init data key")
        values[key] = items[0]

    received_hash = values.pop("hash", None)
    if not received_hash:
        raise InitDataError("missing hash")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise InitDataError("invalid hash")

    try:
        auth_date = int(values.get("auth_date", ""))
    except ValueError as exc:
        raise InitDataError("invalid auth_date") from exc

    current = time.time() if now is None else now
    age = current - auth_date
    if age > max_age_seconds or age < -60:
        raise InitDataError("init data is expired")

    user_raw = values.get("user")
    if not user_raw:
        raise InitDataError("missing user")
    try:
        payload = json.loads(user_raw)
        user_id = int(payload["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise InitDataError("invalid user payload") from exc
    return WebAppUser(
        id=user_id,
        username=payload.get("username"),
        first_name=payload.get("first_name"),
        last_name=payload.get("last_name"),
    )


def _extract_init_data(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("tma "):
        return authorization[4:].strip()
    return request.headers.get("X-Telegram-Init-Data")


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    settings = request.app.state.settings
    init_data = _extract_init_data(request)
    if not init_data:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="initData required")
    try:
        webapp_user = validate_init_data(
            init_data,
            settings.bot_token_value,
            max_age_seconds=settings.init_data_max_age_seconds,
        )
    except InitDataError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid initData"
        ) from exc

    user = await users_service.upsert_telegram_user(
        session,
        TelegramProfile(
            telegram_id=webapp_user.id,
            username=webapp_user.username,
            first_name=webapp_user.first_name,
            last_name=webapp_user.last_name,
        ),
        [],
    )
    if user.role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="access is not granted")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return user
