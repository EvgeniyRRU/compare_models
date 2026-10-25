"""Валидация Telegram Mini App initData (HMAC-SHA256 с ключом от токена бота).

https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

# Допуск на расхождение часов клиента/Telegram и сервера для auth_date «из будущего».
CLOCK_SKEW_SECONDS = 60


class InitDataError(Exception):
    pass


@dataclass(frozen=True)
class InitDataUser:
    id: int
    username: str | None
    first_name: str | None
    last_name: str | None


def secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Подписать поля как это делает Telegram (используется в тестах и локальной отладке)."""
    from urllib.parse import urlencode

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    digest = hmac.new(secret_key(bot_token), data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def validate_init_data(
    init_data: str, bot_token: str, max_age_seconds: int, now: float | None = None
) -> InitDataUser:
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise InitDataError("malformed initData") from exc
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise InitDataError("duplicate keys")
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise InitDataError("hash missing")

    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    expected = hmac.new(secret_key(bot_token), data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("bad signature")

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError) as exc:
        raise InitDataError("auth_date missing") from exc
    current = time.time() if now is None else now
    if current - auth_date > max_age_seconds or auth_date - current > CLOCK_SKEW_SECONDS:
        raise InitDataError("initData expired")

    try:
        user = json.loads(fields["user"])
        return InitDataUser(
            id=int(user["id"]),
            username=user.get("username"),
            first_name=user.get("first_name"),
            last_name=user.get("last_name"),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InitDataError("user missing") from exc
