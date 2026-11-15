"""Tests for Telegram initData validation and the auth header dependency."""

import httpx
import pytest

from app.api.auth import InitData, InvalidInitData, validate_init_data
from app.main import create_app

from .api_helpers import make_init_data, sign_fields

BOT_TOKEN = "test-token"
MAX_AGE = 86_400
AUTH_DATE = 1_700_000_000


def test_valid_init_data_returns_user_id_and_auth_date() -> None:
    init_data = make_init_data(123, auth_date=AUTH_DATE, bot_token=BOT_TOKEN)

    result = validate_init_data(init_data, bot_token=BOT_TOKEN, max_age=MAX_AGE, now=AUTH_DATE)

    assert isinstance(result, InitData)
    assert result.user_id == 123
    assert result.auth_date == AUTH_DATE


def test_tampered_hash_raises() -> None:
    init_data = make_init_data(123, auth_date=AUTH_DATE, bot_token=BOT_TOKEN)
    tampered = init_data[:-1] + ("0" if init_data[-1] != "0" else "1")

    with pytest.raises(InvalidInitData):
        validate_init_data(tampered, bot_token=BOT_TOKEN, max_age=MAX_AGE, now=AUTH_DATE)


def test_missing_hash_raises() -> None:
    init_data = f"auth_date={AUTH_DATE}\nuser=123"

    with pytest.raises(InvalidInitData):
        validate_init_data(init_data, bot_token=BOT_TOKEN, max_age=MAX_AGE, now=AUTH_DATE)


def test_stale_auth_date_raises() -> None:
    init_data = make_init_data(123, auth_date=AUTH_DATE, bot_token=BOT_TOKEN)

    with pytest.raises(InvalidInitData):
        validate_init_data(
            init_data, bot_token=BOT_TOKEN, max_age=MAX_AGE, now=AUTH_DATE + MAX_AGE + 1
        )


def test_missing_user_raises() -> None:
    fields = {"auth_date": str(AUTH_DATE)}
    init_data = f"auth_date={AUTH_DATE}\nhash={sign_fields(fields, BOT_TOKEN)}"

    with pytest.raises(InvalidInitData):
        validate_init_data(init_data, bot_token=BOT_TOKEN, max_age=MAX_AGE, now=AUTH_DATE)


async def test_missing_header_returns_401() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/tickets")

    assert response.status_code == 401
