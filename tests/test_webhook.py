from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.bot.app import build_application
from support_bot.config import Settings
from support_bot.web.app import create_app
from tests.fake_telegram import FakeTelegram
from tests.helpers import add_user

SECRET = "hook-secret"


@pytest.fixture
async def http(
    settings: Settings, engine: Any, session_factory: async_sessionmaker[AsyncSession]
) -> Any:
    settings.webhook_secret_token = SecretStr(SECRET)
    fake = FakeTelegram()
    application = build_application(settings, engine, session_factory, request=fake)
    await application.initialize()
    app = create_app()
    app.state.settings = settings
    app.state.runtime = application.bot_data["runtime"]
    app.state.application = application
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, fake
    await application.shutdown()


def _help_update(update_id: int, tg_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 0,
            "chat": {"id": tg_id, "type": "private"},
            "from": {"id": tg_id, "is_bot": False, "first_name": "u"},
            "text": "/help",
            "entities": [{"type": "bot_command", "offset": 0, "length": 5}],
        },
    }


async def test_wrong_secret_is_forbidden(http: Any) -> None:
    client, _ = http

    response = await client.post(
        "/telegram/webhook",
        json=_help_update(1, 1),
        headers={"X-Telegram-Bot-Api-Secret-Token": "x"},
    )

    assert response.status_code == 403


async def test_duplicate_delivery_is_handled_once(http: Any, session: AsyncSession) -> None:
    client, fake = http
    await add_user(session, 6001, "support")
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}

    first = await client.post("/telegram/webhook", json=_help_update(77, 6001), headers=headers)
    second = await client.post("/telegram/webhook", json=_help_update(77, 6001), headers=headers)

    assert first.status_code == 200 and second.status_code == 200
    help_replies = [m for m in fake.sent() if "Команды" in m.get("text", "")]
    assert len(help_replies) == 1


async def test_processing_error_still_answers_200(
    http: Any, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, _ = http
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}

    async def boom(*_: object, **__: object) -> bool:
        raise RuntimeError("storage down")

    monkeypatch.setattr("support_bot.web.app.process_update_once", boom)
    response = await client.post("/telegram/webhook", json=_help_update(88, 1), headers=headers)

    assert response.status_code == 200


async def test_invalid_json_is_rejected(http: Any) -> None:
    client, _ = http

    response = await client.post(
        "/telegram/webhook",
        content=b"not json",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"},
    )

    assert response.status_code == 400
