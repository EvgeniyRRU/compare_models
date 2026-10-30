import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import User
from app.services.tickets import TicketService

TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/support_bot_test",
)
ROOT = Path(__file__).resolve().parent.parent
INTERVALS = {
    "critical": timedelta(minutes=10),
    "high": timedelta(minutes=30),
    "medium": timedelta(hours=2),
    "low": timedelta(hours=8),
}


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    admin_url = TEST_URL.rsplit("/", 1)[0] + "/postgres"
    dbname = TEST_URL.rsplit("/", 1)[1]
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        exists = (
            await conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": dbname})
        ).scalar()
        if not exists:
            await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    await admin.dispose()
    # схема создаётся миграциями Alembic: заодно проверяем их
    env = {**os.environ, "ALEMBIC_DATABASE_URL": TEST_URL}
    for cmd in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run(
            [sys.executable, "-m", "alembic", *cmd], cwd=ROOT, env=env, check=True, capture_output=True
        )
    eng = create_async_engine(TEST_URL, pool_size=20)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sm(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE users, tickets, ticket_events, ticket_drafts, processed_updates "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def svc() -> TicketService:
    return TicketService(INTERVALS)


async def make_user(sm: async_sessionmaker[AsyncSession], tg_id: int, role: str | None) -> User:
    async with sm() as s, s.begin():
        u = User(telegram_id=tg_id, username=f"u{tg_id}", first_name=f"N{tg_id}", role=role)
        s.add(u)
    return u


@pytest_asyncio.fixture
async def users(sm: async_sessionmaker[AsyncSession]) -> dict[str, User]:
    return {
        "support": await make_user(sm, 1, "support"),
        "dev": await make_user(sm, 2, "developer"),
        "dev2": await make_user(sm, 3, "developer"),
        "admin": await make_user(sm, 4, "admin"),
        "nobody": await make_user(sm, 5, None),
    }
