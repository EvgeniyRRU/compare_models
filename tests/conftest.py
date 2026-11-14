"""Pytest fixtures and shared setup.

Required settings must be present *before* any ``app`` module is imported,
because :mod:`app.config` instantiates a module-level ``Settings`` (and
:mod:`app.db.session` builds the engine) at import time. Pytest imports this
conftest before collecting test modules, so ``setdefault`` here is sufficient.

Tests run against a dedicated ``support_test`` database (created from the
compose PostgreSQL via :data:`app.config.settings.database_url`), migrated with
``alembic upgrade head``, and truncated between tests.
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DEV_CHAT_ID", "1")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://support:support@127.0.0.1:5433/support",
)
os.environ.setdefault("BOOTSTRAP_ADMIN_IDS", "123,456")

from app.config import settings  # noqa: E402

TEST_DB_NAME = "support_test"

_TABLES = (
    "users",
    "tickets",
    "ticket_events",
    "ticket_drafts",
    "processed_updates",
)


def _admin_database_url() -> str:
    """URL of the maintenance DB used to create/drop the test database."""
    return (
        make_url(settings.database_url)
        .set(database="postgres")
        .render_as_string(hide_password=False)
    )


def _test_database_url() -> str:
    return (
        make_url(settings.database_url)
        .set(database=TEST_DB_NAME)
        .render_as_string(hide_password=False)
    )


def _ensure_test_database() -> None:
    """Drop and recreate the isolated test database."""

    async def _run() -> None:
        engine = create_async_engine(_admin_database_url(), isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB_NAME}"'))
                await conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _run_migrations(url: str) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def engine() -> Iterator[AsyncEngine]:
    """A migrated test-database engine shared by the whole session.

    ``NullPool`` keeps every connection on the event loop that created it,
    which matters because pytest-asyncio gives each test a fresh loop.
    """
    _ensure_test_database()
    _run_migrations(_test_database_url())
    test_engine = create_async_engine(_test_database_url(), poolclass=NullPool)
    yield test_engine
    asyncio.run(test_engine.dispose())


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A clean session per test, with all tables truncated up front."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await session.execute(text(f"TRUNCATE TABLE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
        await session.commit()
        yield session
