import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl


class InvalidInitData(Exception):
    pass


def verify_init_data(init_data: str, *, token: str, max_age_seconds: int) -> dict[str, Any]:
    """Validate Telegram WebApp initData (HMAC-SHA256 with the bot token) and return the user."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise InvalidInitData("hash is missing")

    check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InvalidInitData("signature mismatch")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise InvalidInitData("auth_date is malformed") from exc
    if time.time() - auth_date > max_age_seconds:
        raise InvalidInitData("auth_date is too old")

    try:
        user = json.loads(pairs.get("user", ""))
    except json.JSONDecodeError as exc:
        raise InvalidInitData("user is malformed") from exc
    if not isinstance(user, dict) or "id" not in user:
        raise InvalidInitData("user is missing")
    return user
