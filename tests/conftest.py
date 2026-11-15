import os
import subprocess

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("DEV_CHAT_ID", "-1001234567890")
os.environ.setdefault("BOOTSTRAP_ADMIN_IDS", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://rent:rent@localhost:5433/rent_bot_test")
os.environ.setdefault("LOG_FORMAT", "console")

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

TABLES = (
    "processed_updates",
    "ticket_drafts",
    "ticket_events",
    "tickets",
    "users",
)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def _migrated() -> None:
    subprocess.run(["alembic", "upgrade", "head"], check=True)


@pytest.fixture(scope="session")
async def engine() -> AsyncEngine:
    eng = create_async_engine(settings.database_url, pool_size=10)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db_session(session_factory) -> AsyncSession:
    async with session_factory() as s:
        yield s
