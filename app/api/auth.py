"""Telegram WebApp initData validation (pure, no I/O)."""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass


class InvalidInitData(Exception):
    """Raised when initData fails HMAC or freshness validation."""


@dataclass(frozen=True)
class InitData:
    user_id: int
    auth_date: int  # unix seconds


def validate_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age: int,
    now: int | None = None,
) -> InitData:
    pairs: dict[str, str] = {}
    for part in init_data.split("\n"):
        if "=" in part:
            key, _, value = part.partition("=")
            pairs[key] = value

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InvalidInitData("hash missing")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise InvalidInitData("bad signature")

    auth_date = int(pairs.get("auth_date", "0"))
    now = int(time.time()) if now is None else now
    if now - auth_date > max_age:
        raise InvalidInitData("expired")

    user_raw = pairs.get("user")
    if not user_raw:
        raise InvalidInitData("user missing")
    user = json.loads(user_raw)
    return InitData(user_id=int(user["id"]), auth_date=auth_date)
