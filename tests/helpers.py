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
