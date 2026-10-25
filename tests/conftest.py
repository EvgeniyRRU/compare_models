"""Тесты работают на реальном PostgreSQL (см. docker-compose, профиль test).

URL задаётся TEST_DATABASE_URL. База пересоздаётся миграциями Alembic в начале сессии,
перед каждым тестом таблицы очищаются.
"""

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from support_bot.config import Settings
from support_bot.db.engine import SessionFactory, create_session_factory
from support_bot.db.models import User
from support_bot.domain.enums import Priority, Role
from support_bot.domain.validation import TicketData
from support_bot.services.drafts import DraftService
from support_bot.services.ports import Keyboard, PostGoneError
from support_bot.services.post_sync import PostSyncService
from support_bot.services.reminders import ReminderService
from support_bot.services.tickets import TicketService
from support_bot.services.users import UserService
from support_bot.services.views import UserInfo

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:55432/support_bot_test",
)
BOT_TOKEN = "123456:TEST-token"


async def _recreate_database(url: str) -> None:
    sa_url = make_url(url)
    db_name = sa_url.database
    server = sa_url.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(server.render_as_string(hide_password=False))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await conn.close()


def _in_thread(fn: Callable[[], Any]) -> None:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            fn()
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=run)
    t.start()
    t.join()
    if errors:
        raise errors[0]


@pytest.fixture(scope="session")
def database_url() -> str:
    _in_thread(lambda: asyncio.run(_recreate_database(TEST_DATABASE_URL)))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    cfg.attributes["configure_logger"] = False
    _in_thread(lambda: command.upgrade(cfg, "head"))
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        bot_token=BOT_TOKEN,  # type: ignore[arg-type]
        dev_chat_id=-1001,
        database_url=database_url,
        bootstrap_admin_ids=[999],
        post_sync_lock_timeout_seconds=3,
        log_format="console",
    )


@pytest.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(settings.database_url, pool_size=10, max_overflow=20)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def clean_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE users, tickets, ticket_events, ticket_drafts, processed_updates "
                "RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
def sf(engine: AsyncEngine) -> SessionFactory:
    return create_session_factory(engine)


@pytest.fixture
def users(sf: SessionFactory, settings: Settings) -> UserService:
    return UserService(sf, settings.bootstrap_admin_ids)


@pytest.fixture
def tickets(sf: SessionFactory, settings: Settings) -> TicketService:
    return TicketService(sf, settings.reminder_intervals)


@pytest.fixture
def drafts(sf: SessionFactory, tickets: TicketService, settings: Settings) -> DraftService:
    return DraftService(sf, tickets, settings.draft_ttl)


class FakePublisher:
    """Подмена Telegram: хранит посты в памяти, умеет падать и блокироваться."""

    def __init__(self) -> None:
        self.posts: dict[int, tuple[str, Keyboard]] = {}
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, str]] = []
        self.reminders: list[tuple[str, int | None]] = []
        self.next_id = 1000
        self.fail_send: BaseException | None = None
        self.fail_reminder: BaseException | None = None
        self.gone: set[int] = set()
        self.before_return: Callable[[], Awaitable[None]] | None = None

    async def _hook(self) -> None:
        if self.before_return is not None:
            await self.before_return()

    async def send_post(self, text: str, keyboard: Keyboard) -> int:
        if self.fail_send is not None:
            raise self.fail_send
        await self._hook()
        self.next_id += 1
        self.posts[self.next_id] = (text, keyboard)
        self.sent.append((self.next_id, text))
        return self.next_id

    async def edit_post(self, message_id: int, text: str, keyboard: Keyboard) -> None:
        if self.fail_send is not None:
            raise self.fail_send
        if message_id in self.gone or message_id not in self.posts:
            raise PostGoneError()
        await self._hook()
        self.posts[message_id] = (text, keyboard)
        self.edits.append((message_id, text))

    async def send_reminder(self, text: str, reply_to_message_id: int | None) -> None:
        if self.fail_reminder is not None:
            raise self.fail_reminder
        self.reminders.append((text, reply_to_message_id))


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


@pytest.fixture
def post_sync(
    engine: AsyncEngine, sf: SessionFactory, publisher: FakePublisher, settings: Settings
) -> PostSyncService:
    return PostSyncService(
        engine, sf, publisher, lock_timeout=settings.post_sync_lock_timeout_seconds
    )


@pytest.fixture
def reminders(
    sf: SessionFactory, tickets: TicketService, publisher: FakePublisher
) -> ReminderService:
    return ReminderService(sf, tickets, publisher)


type MakeUser = Callable[..., Awaitable[UserInfo]]


@pytest.fixture
def make_user(sf: SessionFactory) -> MakeUser:
    counter = iter(range(10_000, 1_000_000))

    async def factory(role: Role | None = None, **kw: Any) -> UserInfo:
        tg = kw.pop("telegram_id", next(counter))
        async with sf.begin() as s:
            user = (
                await s.execute(
                    insert(User)
                    .values(
                        telegram_id=tg,
                        role=role,
                        first_name=kw.get("first_name", f"U{tg}"),
                        username=kw.get("username"),
                    )
                    .returning(User)
                )
            ).scalar_one()
            return UserInfo.from_model(user)

    return factory


type MakeTicket = Callable[..., Awaitable[int]]


@pytest.fixture
def make_ticket(tickets: TicketService) -> MakeTicket:
    async def factory(author: UserInfo, priority: Priority = Priority.HIGH, **kw: Any) -> int:
        data = TicketData(
            description=kw.get("description", "Не работает оплата"),
            priority=priority,
            request_id=kw.get("request_id", "REQ-1"),
            tenant_id=kw.get("tenant_id"),
        )
        return await tickets.create(author.id, data)

    return factory
