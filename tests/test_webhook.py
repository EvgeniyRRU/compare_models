from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.bot.context import AppContext
from app.config import Settings
from app.db.models import ProcessedUpdate
from app.main import create_app
from app.services.drafts import DraftService
from app.services.post_sync import PostSyncer
from tests.fakes import FakePublisher

SECRET = "s3cret"


class StubApplication:
    bot = None

    def __init__(self):
        self.processed = []
        self.fail = False

    async def process_update(self, update):
        if self.fail:
            raise RuntimeError("boom")
        self.processed.append(update.update_id)


@pytest.fixture
async def env(engine, sm, svc):
    settings = Settings(
        bot_token="1:T", dev_chat_id=-1, webhook_secret=SECRET, log_format="console"
    )
    app = create_app(settings)  # lifespan не запускается: ASGITransport его не вызывает
    pub = FakePublisher()
    app.state.ctx = AppContext(
        settings=settings,
        engine=engine,
        sm=sm,
        tickets=svc,
        drafts=DraftService(svc, timedelta(hours=1)),
        syncer=PostSyncer(engine, sm, pub, -1, 3),
        publisher=pub,
    )
    app.state.application = StubApplication()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, app


UPDATE = {
    "update_id": 500,
    "message": {"message_id": 1, "date": 1, "chat": {"id": 1, "type": "private"}, "text": "hi"},
}
HDR = {"X-Telegram-Bot-Api-Secret-Token": SECRET}


async def test_secret_required(env):
    c, _ = env
    assert (await c.post("/telegram/webhook", json=UPDATE)).status_code == 403
    assert (
        await c.post(
            "/telegram/webhook", json=UPDATE, headers={"X-Telegram-Bot-Api-Secret-Token": "x"}
        )
    ).status_code == 403


async def test_update_processed_once(env):
    c, app = env
    for _ in range(3):
        assert (await c.post("/telegram/webhook", json=UPDATE, headers=HDR)).status_code == 200
    assert app.state.application.processed == [500]


async def test_failure_returns_200_and_keeps_dedup_record(env, sm):
    c, app = env
    app.state.application.fail = True
    assert (await c.post("/telegram/webhook", json=UPDATE, headers=HDR)).status_code == 200
    async with sm() as s:
        assert (
            await s.execute(select(func.count()).select_from(ProcessedUpdate))
        ).scalar_one() == 1
    app.state.application.fail = False
    await c.post("/telegram/webhook", json=UPDATE, headers=HDR)  # повтор не обрабатывается
    assert app.state.application.processed == []


async def test_health(env):
    c, _ = env
    assert (await c.get("/health/live")).json() == {"status": "ok"}
    assert (await c.get("/health/ready")).status_code == 200
