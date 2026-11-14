from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("DEV_CHAT_ID", "-1001234567890")
os.environ.setdefault("BOOTSTRAP_ADMIN_IDS", "")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://support:support@localhost:5433/support_test"
)
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("METRICS_ENABLED", "false")
os.environ.setdefault("WORKERS_ENABLED", "false")

from support_bot.config import Settings  # noqa: E402
from support_bot.db.enums import TicketPriority, UserRole  # noqa: E402
from support_bot.db.models.user import User  # noqa: E402
from support_bot.db.session import create_engine, create_session_factory  # noqa: E402
from support_bot.domain.reminders import ReminderPolicy  # noqa: E402
from support_bot.services import tickets as tickets_service  # noqa: E402

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ["DATABASE_URL"]


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _ensure_database() -> None:
    url = make_url(TEST_DATABASE_URL)
    database = url.database or "support_test"
    admin_dsn = f"postgresql://{url.username}:{url.password}@{url.host}:{url.port or 5432}/postgres"
    connection = await asyncpg.connect(admin_dsn)
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database)
        if not exists:
            safe_name = database.replace('"', '""')
            await connection.execute(f'CREATE DATABASE "{safe_name}"')
    finally:
        await connection.close()


def _run_migrations() -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = TEST_DATABASE_URL
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    await _ensure_database()
    _run_migrations()
    engine = create_engine(TEST_DATABASE_URL)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture(autouse=True)
async def _clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE ticket_events, tickets, ticket_drafts, "
                "processed_updates, users RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def policy() -> ReminderPolicy:
    return ReminderPolicy.from_settings(Settings())


async def create_user(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    telegram_id: int,
    role: UserRole | None,
    first_name: str = "Test",
    username: str | None = None,
) -> User:
    async with session_factory() as session, session.begin():
        user = User(
            telegram_id=telegram_id,
            role=role,
            first_name=first_name,
            username=username,
        )
        session.add(user)
        await session.flush()
    return user


@pytest.fixture
def make_user(session_factory: async_sessionmaker[AsyncSession]):
    async def _make_user(
        *,
        telegram_id: int,
        role: UserRole | None,
        first_name: str = "Test",
        username: str | None = None,
    ) -> User:
        return await create_user(
            session_factory,
            telegram_id=telegram_id,
            role=role,
            first_name=first_name,
            username=username,
        )

    return _make_user


async def create_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    author_id: int,
    policy: ReminderPolicy,
    priority: TicketPriority = TicketPriority.MEDIUM,
    description: str = "Не работает вход в личный кабинет",
    request_id: str = "REQ-1",
    tenant_id: str | None = None,
):
    async with session_factory() as session:
        return await tickets_service.create_ticket(
            session,
            actor_id=author_id,
            description=description,
            priority=priority,
            request_id=request_id,
            tenant_id=tenant_id,
            policy=policy,
        )


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []
        self.edited: list[tuple[int, int, str, object]] = []
        self.reminders: list[tuple[int, str, int | None]] = []
        self.next_message_id = 1000
        self.send_hook = None
        self.edit_hook = None
        self.fail_send = False
        self.edit_missing = False
        self.fail_reminder = False

    async def send_post(self, chat_id: int, text: str, keyboard) -> int:
        call_index = len(self.sent) + 1
        if self.send_hook is not None:
            await self.send_hook(call_index)
        if self.fail_send:
            raise RuntimeError("send failed")
        message_id = self.next_message_id
        self.next_message_id += 1
        self.sent.append((message_id, text, keyboard))
        return message_id

    async def edit_post(self, chat_id: int, message_id: int, text: str, keyboard) -> None:
        if self.edit_hook is not None:
            await self.edit_hook(message_id)
        if self.edit_missing:
            from support_bot.services.post_sync import PostNotFound

            raise PostNotFound("message not found")
        self.edited.append((chat_id, message_id, text, keyboard))

    async def send_reminder(self, chat_id: int, text: str, reply_to_message_id: int | None) -> None:
        if self.fail_reminder:
            raise RuntimeError("reminder failed")
        self.reminders.append((chat_id, text, reply_to_message_id))


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def post_sync(session_factory, publisher: FakePublisher, settings: Settings):
    from support_bot.services.post_sync import PostSyncService

    return PostSyncService(
        session_factory=session_factory,
        publisher=publisher,
        chat_id=settings.dev_chat_id,
        database_dsn=settings.database_dsn,
        lock_timeout=1.0,
    )


@pytest.fixture
def api_app(settings, session_factory, post_sync, policy):
    from fastapi import FastAPI

    from support_bot.api.router import api_router

    app = FastAPI()
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.post_sync = post_sync
    app.state.reminder_policy = policy
    app.include_router(api_router)
    return app


@pytest.fixture
def make_ticket(session_factory: async_sessionmaker[AsyncSession], policy: ReminderPolicy):
    async def _make_ticket(
        *,
        author_id: int,
        priority: TicketPriority = TicketPriority.MEDIUM,
        description: str = "Не работает вход в личный кабинет",
        request_id: str = "REQ-1",
        tenant_id: str | None = None,
    ):
        return await create_ticket(
            session_factory,
            author_id=author_id,
            policy=policy,
            priority=priority,
            description=description,
            request_id=request_id,
            tenant_id=tenant_id,
        )

    return _make_ticket
