import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from support_bot.config import Settings
from support_bot.db.base import Database


@pytest.fixture(autouse=True)
def future_ptb_timedelta(monkeypatch):
    monkeypatch.setenv("PTB_TIMEDELTA", "true")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        bot_token="123456:test_token",
        database_url=os.environ.get(
            "TEST_DATABASE_URL", "postgresql+asyncpg://support:support@localhost:55432/support_test"
        ),
        dev_chat_id=-100123,
        telegram_retry_base_seconds=0,
    )


@pytest.fixture(scope="session")
def migrated_database() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run real PostgreSQL integration tests")
    # Refuse accidental execution against a non-test database.
    if "test" not in url.rsplit("/", 1)[-1]:
        pytest.fail("TEST_DATABASE_URL database name must contain 'test'")
    env = {**os.environ, "DATABASE_URL": url}
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            str(root / ".venv/bin/alembic") if (root / ".venv/bin/alembic").exists() else "alembic",
            "upgrade",
            "head",
        ],
        env=env,
        cwd=root,
        check=True,
    )
    return url


@pytest_asyncio.fixture
async def db(settings: Settings, migrated_database: str) -> AsyncIterator[Database]:
    settings.database_url = migrated_database
    database = Database(settings)
    async with database.engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE ticket_events, ticket_drafts, processed_updates, tickets, users "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield database
    await database.engine.dispose()
