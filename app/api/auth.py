"""Mini App auth: Telegram `initData` HMAC verification + fresh role from DB.

Wire format (Telegram WebApp docs): the raw query string `initDataRaw` contains
percent-encoded `key=value` pairs. `data_check_string` is every pair except
`hash`, values decoded, joined by `\\n` and sorted lexicographically. The secret
key is HMAC-SHA256(`"WebAppData"`, bot token); `hash` must equal
HMAC-SHA256(secret_key, data_check_string) hex. `auth_date` freshness is capped
by AUTH_DATE_MAX_AGE_SECONDS. Roles are always read fresh from DB — revocation
takes effect on the very next request.
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import AuthError
from app.config import Settings
from app.db.models import Role, User
from app.domain.errors import Forbidden
from app.logging_setup import get_logger
from app.services import users as users_svc

log = get_logger("api.auth")

AUTH_HEADER = "Authorization"
INIT_DATA_HEADER = "X-Telegram-Init-Data"
TMA_PREFIX = "tma "


@dataclass(frozen=True)
class AuthUser:
    telegram_id: int
    username: str | None
    full_name: str | None
    role: Role


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def _parse_init_data(init_data: str) -> dict[str, str]:
    # parse_qsl percent-decodes values; keep_blank_values so empty flags survive intact.
    return dict(parse_qsl(init_data, keep_blank_values=True, errors="ignore"))


def _identity(data: dict[str, str]) -> tuple[int, str | None, str | None]:
    user_json = data.get("user")
    if user_json:  # modern initData: one JSON blob
        try:
            user = json.loads(user_json)
        except json.JSONDecodeError:
            raise AuthError("Некорректные данные пользователя") from None
        tid = user.get("id")
        username = user.get("username")
        names = " ".join(str(x) for x in (user.get("first_name"), user.get("last_name")) if x)
    else:  # legacy flat fields (query values are strings)
        try:
            tid = int(data["id"])
        except (KeyError, ValueError):
            raise AuthError("Некорректный telegram id") from None
        username = data.get("username")
        names = " ".join(x for x in (data.get("first_name"), data.get("last_name")) if x)
    if not isinstance(tid, int) or isinstance(tid, bool):
        raise AuthError("Некорректный telegram id")
    return tid, (username or None), (names or None)


def validate_init_data(
    init_data: str, bot_token: str, max_age_seconds: int
) -> tuple[int, str | None, str | None]:
    """Verify hash + freshness, return raw identity (roles are DB's business)."""
    if not init_data:
        raise AuthError()
    data = _parse_init_data(init_data)
    received_hash = data.get("hash", "")
    if not received_hash:
        raise AuthError()

    pairs = sorted(f"{k}={v}" for k, v in data.items() if k != "hash")
    data_check_string = "\n".join(pairs)
    digest = hmac.new(
        _secret_key(bot_token), data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(digest, received_hash):
        raise AuthError("Подпись сессии недействительна")

    try:
        auth_ts = int(data["auth_date"])
    except (KeyError, ValueError):
        raise AuthError("Нет метки времени сессии") from None
    if time.time() - auth_ts > max_age_seconds:
        raise AuthError("Сессия устарела, откройте приложение заново")

    return _identity(data)


async def authenticate(
    init_data: str, session: AsyncSession, cfg: Settings
) -> AuthUser:
    """Full request-time auth: crypto -> registration -> fresh role -> access gate."""
    telegram_id, username, full_name = validate_init_data(
        init_data, cfg.bot_token, cfg.auth_date_max_age_seconds
    )
    user: User = await users_svc.ensure_user(session, telegram_id, username, full_name)
    user = await users_svc.apply_bootstrap_role(session, user, cfg)
    role = await users_svc.get_role(session, telegram_id)
    if role is None:
        raise Forbidden("Доступ к боту выдаёт администратор")
    log.debug("mini_app_auth", telegram_id=telegram_id, role=role.value)
    return AuthUser(telegram_id=telegram_id, username=username, full_name=full_name, role=role)


def _raw_init_data(request: Request) -> str:
    auth = request.headers.get(AUTH_HEADER, "")
    if auth.startswith(TMA_PREFIX):
        return auth[len(TMA_PREFIX) :]
    return request.headers.get(INIT_DATA_HEADER, "")


async def get_current_user(request: Request) -> AuthUser:
    """FastAPI dependency: resolves AuthUser or raises AuthError (401) / Forbidden (403)."""
    cfg: Settings = request.app.state.settings
    session_factory = request.app.state.session_factory
    async with session_factory() as session, session.begin():
        return await authenticate(_raw_init_data(request), session, cfg)
