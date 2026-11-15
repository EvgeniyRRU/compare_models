"""Telegram WebApp initData validation (pure, no I/O)."""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


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
    # Telegram ``WebApp.initData`` is ``&``-separated with URL-encoded values;
    # ``parse_qsl`` splits on ``&`` and percent-decodes each value (the ``user``
    # field is URL-encoded JSON).
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise InvalidInitData("hash missing")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise InvalidInitData("bad signature")

    auth_date = int(pairs.get("auth_date", "0"))
    if auth_date <= 0:
        raise InvalidInitData("auth_date missing")
    now = int(time.time()) if now is None else now
    if not 0 <= now - auth_date <= max_age:
        raise InvalidInitData("expired")

    user_raw = pairs.get("user")
    if not user_raw:
        raise InvalidInitData("user missing")
    user = json.loads(user_raw)
    return InitData(user_id=int(user["id"]), auth_date=auth_date)
