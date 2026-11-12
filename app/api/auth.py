import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import User
from app.db.session import SessionLocal


async def current_user(authorization: str | None = Header(default=None)) -> User:
    token = authorization.removeprefix("tma ") if authorization else ""
    try:
        values = dict(parse_qsl(token, keep_blank_values=True))
        supplied_hash = values.pop("hash")
        auth_date = int(values["auth_date"])
        user_data = json.loads(values["user"])
        data_check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    except KeyError, ValueError, TypeError:
        raise HTTPException(401, "Invalid Telegram initData") from None
    secret_key = hmac.new(b"WebAppData", get_settings().bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()
    now = int(time.time())
    if (
        not hmac.compare_digest(expected, supplied_hash)
        or now - auth_date > get_settings().init_data_max_age_seconds
        or auth_date > now + 30
    ):
        raise HTTPException(401, "Invalid or expired Telegram initData")
    async with SessionLocal.begin() as session:
        user = await session.scalar(select(User).where(User.telegram_id == int(user_data["id"])))
        if user is None or user.role is None:
            raise HTTPException(403, "Роль не назначена")
        user.username = user_data.get("username")
        user.full_name = " ".join(
            filter(None, [user_data.get("first_name"), user_data.get("last_name")])
        )
        from datetime import UTC, datetime

        user.updated_at = datetime.now(UTC)
        session.expunge(user)
        return user
