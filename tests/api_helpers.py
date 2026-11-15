"""Shared helpers for the Mini App REST API tests (Telegram initData signing)."""

import hashlib
import hmac
import json
import time

#: Matches ``BOT_TOKEN`` set in ``tests/conftest.py``.
DEFAULT_BOT_TOKEN = "test-token"


def sign_fields(fields: dict[str, str], bot_token: str = DEFAULT_BOT_TOKEN) -> str:
    """Compute the Telegram initData ``hash`` over the given (sorted) fields."""
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()


def make_init_data(
    user_id: int,
    *,
    auth_date: int,
    bot_token: str = DEFAULT_BOT_TOKEN,
) -> str:
    """Build a valid, freshly-signed ``initData`` string for a Telegram user."""
    user = json.dumps({"id": user_id, "first_name": "Tester"})
    fields = {"auth_date": str(auth_date), "user": user}
    received_hash = sign_fields(fields, bot_token)
    return "\n".join([f"auth_date={auth_date}", f"user={user}", f"hash={received_hash}"])


def auth_headers(user_id: int, *, auth_date: int | None = None) -> dict[str, str]:
    """Return the ``X-Telegram-Init-Data`` header for a fresh initData."""
    if auth_date is None:
        auth_date = int(time.time())
    return {"X-Telegram-Init-Data": make_init_data(user_id, auth_date=auth_date)}
