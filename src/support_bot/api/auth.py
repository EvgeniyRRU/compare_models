"""Telegram Mini App HMAC verification; credentials and DB roles are never cached."""

import hashlib
import hmac
import json
import re
import time
from typing import Annotated, Any
from urllib.parse import parse_qsl

from fastapi import Depends, HTTPException, Request
from structlog.contextvars import bind_contextvars

from support_bot.db.models import User
from support_bot.domain.errors import Forbidden


def verify_init_data(
    raw: str,
    bot_token: str,
    max_age_seconds: int,
    clock_skew_seconds: int = 30,
    now: float | None = None,
) -> dict[str, Any]:
    """Return the authenticated user or raise ValueError, without leaking initData.

    Telegram's bot-token HMAC covers all fields except hash, including signature.
    Excluding signature applies only to the separate third-party Ed25519 scheme.
    """
    if not raw or len(raw) > 16384:
        raise ValueError("Invalid initData")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=64)
    fields = dict(pairs)
    if len(fields) != len(pairs) or any(not key for key in fields):
        raise ValueError("Duplicate or empty initData field")
    received = fields.pop("hash", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", received):
        raise ValueError("Invalid initData hash")
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received.lower()):
        raise ValueError("Invalid initData signature")
    try:
        auth_date = int(fields["auth_date"])
        identity = json.loads(fields["user"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError("Invalid initData identity") from exc
    age = (time.time() if now is None else now) - auth_date
    if age > max_age_seconds or age < -clock_skew_seconds:
        raise ValueError("Expired initData")
    if (
        not isinstance(identity, dict)
        or type(identity.get("id")) is not int
        or not 0 < identity["id"] < 2**63
        or not isinstance(identity.get("first_name"), str)
        or not 0 < len(identity["first_name"]) <= 255
        or any(
            identity.get(key) is not None
            and (not isinstance(identity[key], str) or len(identity[key]) > 255)
            for key in ("username", "last_name")
        )
    ):
        raise ValueError("Invalid initData identity")
    return identity


async def current_user(request: Request) -> User:
    scheme, _, raw = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "tma" or not raw:
        raise HTTPException(status_code=401, detail="Откройте приложение через Telegram")
    settings = request.app.state.settings
    try:
        identity = verify_init_data(
            raw,
            settings.bot_token.get_secret_value(),
            settings.init_data_max_age_seconds,
            settings.auth_clock_skew_seconds,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Недействительные данные Telegram") from exc
    user: User = await request.app.state.users.touch(
        telegram_id=identity["id"],
        first_name=identity["first_name"],
        username=identity.get("username"),
        last_name=identity.get("last_name"),
    )
    bind_contextvars(telegram_user_id=user.telegram_id)
    if user.role not in {"support", "developer", "admin"}:
        raise Forbidden("Доступ не назначен. Обратитесь к администратору")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def admin_user(user: CurrentUser) -> User:
    if user.role != "admin":
        raise Forbidden()
    return user


AdminUser = Annotated[User, Depends(admin_user)]
