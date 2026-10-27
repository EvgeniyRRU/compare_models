from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

pytestmark = pytest.mark.integration


def fake_application():
    return SimpleNamespace(
        bot=SimpleNamespace(),
        initialize=AsyncMock(),
        start=AsyncMock(),
        stop=AsyncMock(),
        shutdown=AsyncMock(),
        process_update=AsyncMock(),
    )


async def test_webhook_health_and_lifespan(db, settings):
    from support_bot.main import create_app

    settings.bot_mode = "webhook"
    settings.webhook_secret = __import__("pydantic").SecretStr("test_secret")
    botapp = fake_application()
    transport = SimpleNamespace(publish=AsyncMock(), send_reminder=AsyncMock())
    app = create_app(
        settings, database=db, application=botapp, transport=transport, enable_workers=False
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 200
            assert (await client.get("/metrics")).status_code == 404
            assert (await client.get("/")).status_code == 200
            assert (
                await client.post("/telegram/webhook", json={"update_id": 1})
            ).status_code == 403
            headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret"}
            first = await client.post("/telegram/webhook", json={"update_id": 1}, headers=headers)
            second = await client.post("/telegram/webhook", json={"update_id": 1}, headers=headers)
            assert first.status_code == second.status_code == 200
            assert first.headers.get("x-request-id")
            assert botapp.process_update.await_count == 1
    botapp.initialize.assert_awaited_once()
    botapp.start.assert_awaited_once()
    botapp.stop.assert_awaited_once()
    botapp.shutdown.assert_awaited_once()


async def test_webhook_handler_failure_still_200(db, settings):
    from pydantic import SecretStr

    from support_bot.main import create_app

    settings.bot_mode = "webhook"
    settings.webhook_secret = SecretStr("test_secret")
    botapp = fake_application()
    botapp.process_update.side_effect = ValueError("boom")
    app = create_app(
        settings, database=db, application=botapp, transport=SimpleNamespace(), enable_workers=False
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/telegram/webhook",
            json={"update_id": 22},
            headers={"X-Telegram-Bot-Api-Secret-Token": "test_secret"},
        )
        assert response.status_code == 200
        await client.post(
            "/telegram/webhook",
            json={"update_id": 22},
            headers={"X-Telegram-Bot-Api-Secret-Token": "test_secret"},
        )
        assert botapp.process_update.await_count == 1
