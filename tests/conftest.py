"""Тесты на реальном PostgreSQL из docker compose.

- новая БД на сессию (drop/create + alembic upgrade head);
- TRUNCATE всех таблиц между тестами (конкурентные тесты требуют реальных коммитов);
- RecordingBot вместо сетевых вызовов Telegram.
"""

import asyncio
import os

import asyncpg
import pytest
import pytest_asyncio

from app.config import Settings
from app.db import make_db
from tests.fakes import RecordingBot

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://bot:bot@localhost:5432/rent_bot_test"
)
FAKE_TOKEN = "123456789:AAHtesttesttesttesttesttesttesttest1"


def _split(url: str) -> tuple[str, str]:
    prefix, db_name = url.rsplit("/", 1)
    return prefix, db_name


def _create_test_database() -> None:
    prefix, db_name = _split(TEST_DB_URL)

    async def _run() -> None:
        dsn = prefix.replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                db_name,
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
            await conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await conn.close()

    asyncio.run(_run())
    _run_alembic()


def _run_alembic() -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    cfg.set_main_option("sqlalchemy.url", TEST_DB_URL)
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def test_database():
    _create_test_database()
    yield


@pytest_asyncio.fixture
async def db():
    db = make_db(TEST_DB_URL)
    yield db
    await db.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(db):
    yield
    from sqlalchemy import text

    async with db.session_factory() as session, session.begin():
        await session.execute(
            text(
                "TRUNCATE users, tickets, ticket_events, ticket_drafts, "
                "processed_updates RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bot_token=FAKE_TOKEN,
        bot_mode="polling",
        dev_chat_id=-1001234567890,
        bootstrap_admin_ids=[111],
        database_url=TEST_DB_URL,
        mini_app_url="https://mini.example/",
        log_format="json",
        sentry_dsn=None,
        _env_file=None,
    )


@pytest.fixture
def bot() -> RecordingBot:
    return RecordingBot(FAKE_TOKEN)


@pytest_asyncio.fixture
async def tg_app(settings, db, bot):
    """PTB-приложение с RecordingBot (без сети). Используется тестами Stage 5+."""
    # NB: PTB 22.8 start() НЕ вызывает post_init (только run_polling/run_webhook) —
    # bot.commands / bot.webhook под tg_app не устанавливаются
    from app.bot.application import build_application

    app = build_application(settings, db, bot=bot)
    await app.initialize()
    await app.start()
    yield app
    await app.stop()
    await app.shutdown()
