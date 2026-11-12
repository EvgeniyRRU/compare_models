import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from app.api.auth import current_user
from app.core.config import get_settings
from app.db.models import Role, User
from app.db.session import SessionLocal


@pytest.mark.asyncio
async def test_init_data_rejects_tampering():
    with pytest.raises(HTTPException) as error:
        await current_user("tma auth_date=1&user=%7B%7D&hash=bad")
    assert error.value.status_code == 401


def signed_init_data(user: dict) -> str:
    fields = {"auth_date": str(int(time.time())), "user": json.dumps(user, separators=(",", ":"))}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", get_settings().bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return "tma " + urlencode(fields)


@pytest.mark.asyncio
async def test_init_data_resolves_current_database_role():
    telegram_id = int(time.time() * 1000)
    async with SessionLocal.begin() as session:
        user = User(telegram_id=telegram_id, role=Role.SUPPORT, full_name="Test User")
        session.add(user)
        await session.flush()
        user_id = user.id
    try:
        signed = signed_init_data({"id": telegram_id, "first_name": "Test", "last_name": "User"})
        authenticated = await current_user(signed)
        assert authenticated.id == user_id
        assert authenticated.role == Role.SUPPORT
    finally:
        async with SessionLocal.begin() as session:
            await session.delete(await session.get(User, user_id))
