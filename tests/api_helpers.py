"""Shared helpers for the Mini App REST API tests (Telegram initData signing)."""

import hashlib
import hmac
import json
import time
import urllib.parse

#: Matches ``BOT_TOKEN`` set in ``tests/conftest.py``.
DEFAULT_BOT_TOKEN = "test-token"


def sign_fields(fields: dict[str, str], bot_token: str) -> str:
    """Compute the Telegram initData ``hash`` over the given (sorted) fields."""
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()


def build_init_data(
    bot_token: str,
    *,
    user_id: int,
    auth_date: int,
    extra: dict[str, str] | None = None,
) -> str:
    """Build a signed, URL-encoded Telegram ``initData`` string.

    Telegram's ``WebApp.initData`` is ``&``-separated with URL-encoded values,
    ending in a ``hash=...`` field computed over the ``\\n``-joined sorted
    ``key=value`` pairs of every other field.
    """
    fields: dict[str, str] = {"auth_date": str(auth_date), "user": json.dumps({"id": user_id})}
    if extra:
        fields.update(extra)
    fields["hash"] = sign_fields(fields, bot_token)
    return urllib.parse.urlencode(fields)


def auth_headers(user_id: int, *, auth_date: int | None = None) -> dict[str, str]:
    """Return the ``X-Telegram-Init-Data`` header for a fresh initData."""
    if auth_date is None:
        auth_date = int(time.time())
    return {
        "X-Telegram-Init-Data": build_init_data(
            DEFAULT_BOT_TOKEN, user_id=user_id, auth_date=auth_date
        )
    }
