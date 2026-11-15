"""initData HMAC validation + request-time auth dependency."""

import json
import time
from urllib.parse import quote

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import authenticate, validate_init_data
from app.api.errors import AuthError
from app.config import settings
from app.db.models import Role, User
from app.domain.errors import Forbidden
from tests.conftest import sign_init_data, user_pairs

TOKEN = settings.bot_token
MAX_AGE = settings.auth_date_max_age_seconds


def test_valid_modern_init_data() -> None:
    raw = sign_init_data(user_pairs(101, "dev_one", "Dev One"), token=TOKEN)
    tid, username, full_name = validate_init_data(raw, TOKEN, MAX_AGE)
    assert (tid, username, full_name) == (101, "dev_one", "Dev One")


def test_valid_legacy_flat_fields() -> None:
    raw = sign_init_data(
        {"id": "777", "first_name": "Ivan", "last_name": "Petrov", "username": "ivan"},
        token=TOKEN,
    )
    tid, username, full_name = validate_init_data(raw, TOKEN, MAX_AGE)
    assert (tid, username, full_name) == (777, "ivan", "Ivan Petrov")


def test_unicode_values_round_trip() -> None:
    raw = sign_init_data(user_pairs(102, "ivan_p", "Иван Петров"), token=TOKEN)
    assert validate_init_data(raw, TOKEN, MAX_AGE)[2] == "Иван Петров"


def test_tampered_hash_rejected() -> None:
    raw = sign_init_data(user_pairs(101), token=TOKEN)
    tampered = raw.replace("hash=", "hash=deadbeef")
    with pytest.raises(AuthError, match="Подпись"):
        validate_init_data(tampered, TOKEN, MAX_AGE)


def test_extra_pair_breaks_signature() -> None:
    """Injecting another id after signing changes the data_check_string."""
    raw = sign_init_data(user_pairs(101), token=TOKEN) + "&id=999"
    with pytest.raises(AuthError):
        validate_init_data(raw, TOKEN, MAX_AGE)


def test_wrong_token_rejected() -> None:
    raw = sign_init_data(user_pairs(101), token="123:OTHER-BOT")
    with pytest.raises(AuthError):
        validate_init_data(raw, TOKEN, MAX_AGE)


def test_expired_auth_date_rejected() -> None:
    raw = sign_init_data(user_pairs(101), token=TOKEN, auth_age=MAX_AGE + 3600)
    with pytest.raises(AuthError, match="устарела"):
        validate_init_data(raw, TOKEN, MAX_AGE)


def test_fresh_boundary_accepted() -> None:
    raw = sign_init_data(user_pairs(101), token=TOKEN, auth_age=MAX_AGE - 60)
    assert validate_init_data(raw, TOKEN, MAX_AGE)[0] == 101


def test_missing_hash_rejected() -> None:
    raw = "auth_date=" + str(int(time.time())) + "&user=" + quote(
        json.dumps({"id": 5}), safe=""
    )
    with pytest.raises(AuthError):
        validate_init_data(raw, TOKEN, MAX_AGE)


def test_missing_auth_date_rejected() -> None:
    import hashlib
    import hmac
    from urllib.parse import urlencode as enc

    data = {"user": json.dumps({"id": 5})}
    check = "\n".join(sorted(f"{k}={v}" for k, v in data.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(AuthError, match="метки времени"):
        validate_init_data(enc({**data, "hash": digest}), TOKEN, MAX_AGE)


def test_empty_init_data_rejected() -> None:
    with pytest.raises(AuthError):
        validate_init_data("", TOKEN, MAX_AGE)


async def test_authenticate_registers_then_gates_unknown_role(db_session: AsyncSession) -> None:
    raw = sign_init_data(user_pairs(901, "newbie", "New Bie"))
    with pytest.raises(Forbidden):
        await authenticate(raw, db_session, settings)
    await db_session.commit()
    role = await db_session.scalar(select(User.role).where(User.telegram_id == 901))
    assert role is None


async def test_authenticate_reads_role_fresh_from_db(db_session: AsyncSession) -> None:
    raw = sign_init_data(user_pairs(902, "dev902", "Dev 902"))
    with pytest.raises(Forbidden):
        await authenticate(raw, db_session, settings)
    await db_session.commit()

    await db_session.execute(
        update(User).where(User.telegram_id == 902).values(role=Role.developer)
    )
    await db_session.commit()

    user = await authenticate(raw, db_session, settings)
    assert user.role is Role.developer
    assert user.telegram_id == 902


async def test_authenticate_bootstrap_admin(db_session: AsyncSession) -> None:
    # conftest sets BOOTSTRAP_ADMIN_IDS=1: id 1 becomes admin on first auth.
    raw = sign_init_data(user_pairs(1, "boss", "The Boss"))
    user = await authenticate(raw, db_session, settings)
    assert user.role is Role.admin
