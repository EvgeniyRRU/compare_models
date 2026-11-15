"""Tests for the Telegram webhook endpoint (real PostgreSQL for dedup)."""

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import Bot, Update

from app.config import settings
from app.db.models import ProcessedUpdate
from app.main import create_app

SECRET = "test-secret"


class FakeApplication:
    """Minimal stand-in for the PTB ``Application`` used by the webhook.

    Exposes just enough surface for ``Update.de_json(payload, bot)`` (a ``.bot``)
    and the webhook's ``await process_update(update)`` (an async method that
    records the update, optionally raising).
    """

    def __init__(self) -> None:
        self.bot = Bot(token="123456:TEST")
        self.processed: list[int] = []
        self.raise_on_process: Exception | None = None
        self.sleep_duration: float = 0.0

    async def process_update(self, update: Update) -> None:
        if self.sleep_duration:
            await asyncio.sleep(self.sleep_duration)
        if self.raise_on_process is not None:
            raise self.raise_on_process
        self.processed.append(update.update_id)


@pytest.fixture
def webhook_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _redirect_webhook_dedup(
    monkeypatch: pytest.MonkeyPatch,
    webhook_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Point the webhook's dedup session factory at the isolated test engine.
    monkeypatch.setattr("app.webhook.async_session_factory", webhook_session_factory)


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "webhook_secret", SECRET)
    return SECRET


def make_client(fake: FakeApplication, engine: AsyncEngine) -> TestClient:
    app = create_app(engine=engine, application=fake)  # type: ignore[arg-type]
    return TestClient(app)


def text_message_payload(update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "from": {"id": 1, "is_bot": False, "first_name": "Tester"},
            "chat": {"id": 1, "type": "private"},
            "date": 1,
            "text": "hello",
        },
    }


async def dedup_ids(
    factory: async_sessionmaker[AsyncSession],
) -> list[int]:
    async with factory() as session:
        result = await session.execute(select(ProcessedUpdate.update_id))
        return list(result.scalars())


async def test_missing_secret_returns_401(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    secret: str,
    session: AsyncSession,
) -> None:
    fake = FakeApplication()
    client = make_client(fake, engine)

    response = client.post("/telegram/webhook", json=text_message_payload(1))

    assert response.status_code == 401
    assert fake.processed == []
    assert await dedup_ids(webhook_session_factory) == []


async def test_wrong_secret_returns_401(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    secret: str,
    session: AsyncSession,
) -> None:
    fake = FakeApplication()
    client = make_client(fake, engine)

    response = client.post(
        "/telegram/webhook",
        json=text_message_payload(1),
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )

    assert response.status_code == 401
    assert fake.processed == []
    assert await dedup_ids(webhook_session_factory) == []


async def test_correct_secret_processes_once_and_records_dedup(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    secret: str,
    session: AsyncSession,
) -> None:
    fake = FakeApplication()
    client = make_client(fake, engine)

    response = client.post(
        "/telegram/webhook",
        json=text_message_payload(42),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )

    assert response.status_code == 200
    assert fake.processed == [42]
    assert await dedup_ids(webhook_session_factory) == [42]


async def test_duplicate_update_id_is_noop(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    secret: str,
    session: AsyncSession,
) -> None:
    fake = FakeApplication()
    client = make_client(fake, engine)
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    payload = text_message_payload(7)

    first = client.post("/telegram/webhook", json=payload, headers=headers)
    second = client.post("/telegram/webhook", json=payload, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert fake.processed == [7]  # processed once, not again on the retry
    assert await dedup_ids(webhook_session_factory) == [7]


async def test_process_error_still_returns_200_and_commits_dedup(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    secret: str,
    session: AsyncSession,
) -> None:
    fake = FakeApplication()
    fake.raise_on_process = RuntimeError("boom")
    client = make_client(fake, engine)

    response = client.post(
        "/telegram/webhook",
        json=text_message_payload(99),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )

    assert response.status_code == 200
    assert fake.processed == []
    assert await dedup_ids(webhook_session_factory) == [99]  # marker committed despite the error


async def test_malformed_body_returns_200(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    secret: str,
    session: AsyncSession,
) -> None:
    fake = FakeApplication()
    client = make_client(fake, engine)

    response = client.post(
        "/telegram/webhook",
        content=b"not json",
        headers={
            "X-Telegram-Bot-Api-Secret-Token": SECRET,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert fake.processed == []
    assert await dedup_ids(webhook_session_factory) == []


async def test_missing_secret_header_when_no_secret_configured_allows(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no secret configured, the header is not required and processing runs.
    monkeypatch.setattr(settings, "webhook_secret", "")
    fake = FakeApplication()
    client = make_client(fake, engine)

    response = client.post("/telegram/webhook", json=text_message_payload(5))

    assert response.status_code == 200
    assert fake.processed == [5]
    assert await dedup_ids(webhook_session_factory) == [5]


async def test_invalid_structure_returns_200_without_dedup(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    secret: str,
    session: AsyncSession,
) -> None:
    fake = FakeApplication()
    client = make_client(fake, engine)

    response = client.post(
        "/telegram/webhook",
        json={},
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )

    assert response.status_code == 200
    assert fake.processed == []
    assert await dedup_ids(webhook_session_factory) == []


async def test_process_timeout_returns_200_and_commits_dedup(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    secret: str,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Timeout far shorter than the handler's sleep: ``asyncio.wait_for`` raises
    # ``TimeoutError``, which is caught, and the dedup marker must still survive.
    monkeypatch.setattr(settings, "webhook_update_timeout", 0.05)
    fake = FakeApplication()
    fake.sleep_duration = 0.5
    client = make_client(fake, engine)

    response = client.post(
        "/telegram/webhook",
        json=text_message_payload(11),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )

    assert response.status_code == 200
    assert fake.processed == []
    assert await dedup_ids(webhook_session_factory) == [11]  # marker committed before processing


async def test_no_application_wired_returns_503(
    engine: AsyncEngine,
    webhook_session_factory: async_sessionmaker[AsyncSession],
    secret: str,
    session: AsyncSession,
) -> None:
    # ``application=None`` (placeholder lifespan) must 503 so Telegram redelivers,
    # not fall through to an ``AttributeError`` on ``application.bot``.
    client = TestClient(create_app(engine=engine, application=None))

    response = client.post(
        "/telegram/webhook",
        json=text_message_payload(13),
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )

    assert response.status_code == 503
    assert await dedup_ids(webhook_session_factory) == []
