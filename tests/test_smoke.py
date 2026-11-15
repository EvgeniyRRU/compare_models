from sqlalchemy import text

from app.config import settings


def test_settings_defaults() -> None:
    assert settings.bot_mode == "polling"
    assert settings.dev_chat_id == -1001234567890
    assert settings.bootstrap_admin_ids == [1]


async def test_postgres_connection(db_session) -> None:
    result = await db_session.execute(text("SELECT 1"))
    assert result.scalar() == 1
