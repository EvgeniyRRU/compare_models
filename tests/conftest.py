import hashlib
import hmac
import os
import subprocess
import time
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("DEV_CHAT_ID", "-1001234567890")
os.environ.setdefault("BOOTSTRAP_ADMIN_IDS", "1")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://rent:rent@localhost:5433/rent_bot_test")
os.environ.setdefault("LOG_FORMAT", "console")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.main import create_app
from tests.fake_gateway import FakeGateway


def sign_init_data(pairs: dict[str, str], *, token: str | None = None, auth_age: int = 0) -> str:
    """Build a valid initDataRaw (percent-encoded), Telegram-style HMAC-SHA256."""
    bot_token = token or settings.bot_token
    data = dict(pairs)
    data["auth_date"] = str(int(time.time()) - auth_age)
    check = "\n".join(sorted(f"{k}={v}" for k, v in data.items() if k != "hash"))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def tma_header(pairs: dict[str, str], **kw: object) -> dict[str, str]:
    return {"Authorization": "tma " + sign_init_data(pairs, **kw)}  # type: ignore[arg-type]


def user_pairs(
    telegram_id: int, username: str = "tester", full_name: str = "Test User"
) -> dict[str, str]:
    import json

    return {
        "user": json.dumps(
            {"id": telegram_id, "first_name": full_name, "username": username},
            ensure_ascii=False,
        )
    }


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


@pytest.fixture
async def api_app(session_factory) -> object:
    """Bot-less ASGI app wired to the test DB and a FakeGateway for post sync."""
    app = create_app(bot_enabled=False)
    app.state.session_factory = session_factory
    app.state.gateway = FakeGateway()
    return app


@pytest.fixture
async def api_client(api_app) -> AsyncClient:
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
