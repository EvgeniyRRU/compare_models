"""Валидация initData Mini App (HMAC-SHA256 по документации Telegram)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class AuthError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int) -> dict[str, Any]:
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    hash_value = params.pop("hash", None)
    if not hash_value:
        raise AuthError("hash missing")
    auth_date_raw = params.get("auth_date")
    if auth_date_raw is None:
        raise AuthError("auth_date missing")
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        raise AuthError("invalid auth_date") from None
    if abs(time.time() - auth_date) > max_age_seconds:
        raise AuthError("auth_date too old")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(bot_token.encode(), b"WebAppData", hashlib.sha256).digest()
    digest = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(digest, hash_value):
        raise AuthError("hash mismatch")
    user_raw = params.get("user")
    if not user_raw:
        raise AuthError("user missing")
    try:
        user: dict[str, Any] = json.loads(user_raw)
    except json.JSONDecodeError:
        raise AuthError("invalid user json") from None
    return user
