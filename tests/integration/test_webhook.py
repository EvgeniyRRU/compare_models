# tests/integration/test_webhook.py
import httpx
import pytest_asyncio
from fastapi import FastAPI

from app.bot.webhook import router as webhook_router
from app.config import Settings
from app.services.updates import register_update
from tests.conftest import FAKE_TOKEN, TEST_DB_URL
from tests.fakes import make_update

SECRET = "s3cret"


@pytest_asyncio.fixture
async def wh_client(db, bot):
    from app.bot.application import build_application

    settings = Settings(
        bot_token=FAKE_TOKEN,
        bot_mode="webhook",
        webhook_url="https://example.com/telegram/webhook",
        webhook_secret_token=SECRET,
        dev_chat_id=-1001234567890,
        bootstrap_admin_ids=[111],
        database_url=TEST_DB_URL,
        mini_app_url="",
        _env_file=None,
    )
    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    tg = build_application(settings, db, bot=bot)
    await tg.initialize()
    await tg.start()
    app.state.tg_app = tg
    app.include_router(webhook_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await tg.stop()
    await tg.shutdown()


async def test_bad_token_rejected(wh_client):
    r = await wh_client.post(
        "/telegram/webhook",
        json={"update_id": 1},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert r.status_code == 403


async def test_missing_token_rejected(wh_client):
    r = await wh_client.post("/telegram/webhook", json={"update_id": 1})
    assert r.status_code == 403


async def test_update_processed(wh_client, bot):
    payload = make_update(1, 111, text="/start")
    r = await wh_client.post(
        "/telegram/webhook", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert len(bot.sent) == 1


async def test_duplicate_update_skipped(wh_client, bot):
    payload = make_update(2, 111, text="/start")
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    r1 = await wh_client.post("/telegram/webhook", json=payload, headers=headers)
    r2 = await wh_client.post("/telegram/webhook", json=payload, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert len(bot.sent) == 1


async def test_processing_error_still_200_dedup_kept(wh_client, bot, db):
    """Ошибка обработки: ответ 200, запись дедупликации не откатывается."""
    payload = make_update(3, 111, text="/start")
    bot.fail_next_send = RuntimeError("telegram down")
    r = await wh_client.post(
        "/telegram/webhook", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
    )
    assert r.status_code == 200
    assert await register_update(db, 3) is False
