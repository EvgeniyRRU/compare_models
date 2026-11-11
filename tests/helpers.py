from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


async def add_user(session: AsyncSession, telegram_id: int, role: str | None) -> int:
    user_id = await session.scalar(
        text(
            "INSERT INTO users (telegram_id, full_name, role) "
            "VALUES (:tg, :name, :role) "
            "ON CONFLICT (telegram_id) DO UPDATE SET role = EXCLUDED.role "
            "RETURNING id"
        ),
        {"tg": telegram_id, "name": f"user{telegram_id}", "role": role},
    )
    await session.commit()
    assert user_id is not None
    return int(user_id)


def signed_init_data(telegram_id: int, token: str = "1:test", auth_date: int | None = None) -> str:
    """Build Telegram WebApp initData signed with the bot token, as the Mini App sends it."""
    import hashlib
    import hmac
    import json
    import time
    from urllib.parse import urlencode

    params = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "user": json.dumps(
            {"id": telegram_id, "first_name": f"u{telegram_id}"}, separators=(",", ":")
        ),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**params, "hash": digest})
