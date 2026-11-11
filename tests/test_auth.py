import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from support_bot.web.auth import InvalidInitData, verify_init_data

TOKEN = "123:secret"


def _sign(params: dict[str, str], token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**params, "hash": digest})


def _init_data(auth_date: int, user_id: int = 42) -> str:
    params = {
        "auth_date": str(auth_date),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Ivan"}, separators=(",", ":")),
    }
    return _sign(params)


def test_valid_signature_returns_telegram_user() -> None:
    init_data = _init_data(int(time.time()), user_id=77)

    user = verify_init_data(init_data, token=TOKEN, max_age_seconds=3600)

    assert user["id"] == 77


def test_tampered_payload_is_rejected() -> None:
    init_data = _init_data(int(time.time()), user_id=77).replace("77", "78")

    with pytest.raises(InvalidInitData):
        verify_init_data(init_data, token=TOKEN, max_age_seconds=3600)


def test_signature_from_another_bot_is_rejected() -> None:
    params = {"auth_date": str(int(time.time())), "user": '{"id":1}'}
    init_data = _sign(params, token="999:other")

    with pytest.raises(InvalidInitData):
        verify_init_data(init_data, token=TOKEN, max_age_seconds=3600)


def test_stale_auth_date_is_rejected() -> None:
    init_data = _init_data(int(time.time()) - 7200)

    with pytest.raises(InvalidInitData):
        verify_init_data(init_data, token=TOKEN, max_age_seconds=3600)


def test_missing_hash_is_rejected() -> None:
    with pytest.raises(InvalidInitData):
        verify_init_data("auth_date=1&user=%7B%7D", token=TOKEN, max_age_seconds=3600)
