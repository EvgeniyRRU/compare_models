from __future__ import annotations

import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from telegram import Bot

from support_bot.db.models.processed_update import ProcessedUpdate


class FakeApplication:
    def __init__(self) -> None:
        self.bot = Bot("123456:ABCDEFabcdef")
        self.processed: list[object] = []
        self.fail = False

    async def process_update(self, update) -> None:
        self.processed.append(update)
        if self.fail:
            raise RuntimeError("processing failed")


@pytest.fixture
def fake_application(api_app) -> FakeApplication:
    application = FakeApplication()
    api_app.state.bot_application = application
    return application


@pytest.fixture
async def webhook_client(api_app):
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _payload(update_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "date": int(time.time()),
            "chat": {"id": 777, "type": "private"},
            "from": {
                "id": 777,
                "is_bot": False,
                "first_name": "Test",
                "username": "tester",
            },
            "text": "/start",
        },
    }


async def test_webhook_requires_secret(webhook_client, fake_application) -> None:
    response = await webhook_client.post("/telegram/webhook", json=_payload(1))
    assert response.status_code == 403
    assert fake_application.processed == []


async def test_webhook_deduplicates_updates(webhook_client, fake_application) -> None:
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
    first = await webhook_client.post("/telegram/webhook", json=_payload(10), headers=headers)
    second = await webhook_client.post("/telegram/webhook", json=_payload(10), headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(fake_application.processed) == 1


async def test_webhook_error_keeps_dedup_record(
    webhook_client, fake_application, session_factory
) -> None:
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
    fake_application.fail = True
    response = await webhook_client.post("/telegram/webhook", json=_payload(11), headers=headers)
    assert response.status_code == 200
    assert len(fake_application.processed) == 1

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(ProcessedUpdate).where(ProcessedUpdate.update_id == 11)
        )
    assert count == 1

    fake_application.fail = False
    response = await webhook_client.post("/telegram/webhook", json=_payload(11), headers=headers)
    assert response.status_code == 200
    assert len(fake_application.processed) == 1


async def test_webhook_ignores_payload_without_update_id(webhook_client, fake_application) -> None:
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test-secret"}
    response = await webhook_client.post("/telegram/webhook", json={"foo": "bar"}, headers=headers)
    assert response.status_code == 200
    assert fake_application.processed == []
