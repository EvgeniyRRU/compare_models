import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, Request

from app.bot.context import AppContext
from app.db.models import User
from app.domain.state_machine import Role
from app.services.users import ensure_user


class InvalidInitData(Exception):
    pass


@dataclass(frozen=True)
class TgIdentity:
    id: int
    username: str | None
    first_name: str | None
    last_name: str | None


def validate_init_data(
    init_data: str, bot_token: str, max_age: int, now: float | None = None
) -> TgIdentity:
    """HMAC-SHA256: secret = HMAC("WebAppData", bot_token); проверка свежести auth_date."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise InvalidInitData("no hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InvalidInitData("bad hash")
    try:
        auth_date = int(pairs["auth_date"])
        user = json.loads(pairs["user"])
        user_id = int(user["id"])
    except (KeyError, ValueError, TypeError) as e:
        raise InvalidInitData("malformed") from e
    if (now if now is not None else time.time()) - auth_date > max_age:
        raise InvalidInitData("expired")
    return TgIdentity(user_id, user.get("username"), user.get("first_name"), user.get("last_name"))


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def current_user(ctx: Ctx, authorization: Annotated[str | None, Header()] = None) -> User:
    """Авторизация: `Authorization: tma <initData>`; роль берётся из БД при каждом запросе."""
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Нет авторизации")
    try:
        ident = validate_init_data(
            authorization[4:], ctx.settings.bot_token, ctx.settings.initdata_max_age_seconds
        )
    except InvalidInitData as e:
        raise HTTPException(401, "Недействительные данные авторизации") from e
    async with ctx.sm() as s, s.begin():
        user = await ensure_user(
            s,
            ident.id,
            ident.username,
            ident.first_name,
            ident.last_name,
            ctx.settings.bootstrap_admin_ids,
        )
    if user.role is None:
        raise HTTPException(403, "Нет доступа: обратитесь к администратору")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def admin_user(user: CurrentUser) -> User:
    if user.role != Role.ADMIN.value:
        raise HTTPException(403, "Только для админа")
    return user


AdminUser = Annotated[User, Depends(admin_user)]
