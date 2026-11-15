"""Webhook endpoint and polling loop: auth, dedup collapse, resilience, offsets."""

import asyncio

import httpx

from app.config import settings
from app.main import create_app
from app.polling import poll_once, run_polling
from app.services import dedup
from tests.test_dedup import MSG, Recorder, make_update


def update_payload(update_id: int) -> dict:
    return {"update_id": update_id, "message": MSG}


def build_app(session_factory, settings_copy, processor):
    app = create_app(settings_copy)
    app.state.session_factory = session_factory
    app.state.update_processor = processor
    return app


async def make_client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


WRONG_SECRET = {"X-Telegram-Bot-Api-Secret-Token": "wrong"}
GOOD_SECRET = {"X-Telegram-Bot-Api-Secret-Token": "topsecret"}


async def test_secret_required_and_constant_time(session_factory) -> None:
    cfg = settings.model_copy(update={"webhook_secret_token": "topsecret"})
    rec = Recorder()
    app = build_app(session_factory, cfg, rec)
    client = await make_client(app)
    async with client:
        r = await client.post("/telegram/webhook", json=update_payload(201))
        assert r.status_code == 403
        r = await client.post(
            "/telegram/webhook", json=update_payload(201), headers=WRONG_SECRET
        )
        assert r.status_code == 403
        r = await client.post(
            "/telegram/webhook", json=update_payload(201), headers=GOOD_SECRET
        )
        assert r.status_code == 200
        assert rec.seen == [201]
        # Duplicate delivery: processed once, still 200.
        r = await client.post(
            "/telegram/webhook", json=update_payload(201), headers=GOOD_SECRET
        )
        assert r.status_code == 200
        assert rec.seen == [201]


async def test_always_200_even_on_errors(session_factory) -> None:
    rec = Recorder(fail_ids={300})
    app = build_app(session_factory, settings, rec)
    client = await make_client(app)
    async with client:
        r = await client.post("/telegram/webhook", json=update_payload(300))
        assert r.status_code == 200  # known-bad update must not be retried forever
        assert rec.seen == [300]
        r = await client.post("/telegram/webhook", json={"update_id": 301, "nonsense": True})
        assert r.status_code == 200  # unparsable body dropped
        r = await client.request(
            "POST", "/telegram/webhook", content=b"not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 200


async def test_webhook_without_processor_drops(session_factory) -> None:
    app = create_app(settings)
    app.state.session_factory = session_factory
    app.state.update_processor = None
    client = await make_client(app)
    async with client:
        r = await client.post("/telegram/webhook", json=update_payload(400))
        assert r.status_code == 200
        assert r.json().get("dropped") == "processor_not_configured"


async def test_poll_once_advances_offset(session_factory) -> None:
    class FakeSource:
        def __init__(self, batches):
            self.batches = list(batches)
            self.offsets: list[int] = []

        async def get_updates(self, offset: int):
            self.offsets.append(offset)
            return self.batches.pop(0) if self.batches else []

        async def delete_webhook(self) -> None:
            pass

    rec = Recorder()
    source = FakeSource([[make_update(501), make_update(502)], []])
    offset = await poll_once(
        source=source,
        processor=rec,
        session_factory=session_factory,
        settings=settings,
        offset=501,
    )
    assert offset == 503
    assert rec.seen == [501, 502]
    offset2 = await poll_once(
        source=source,
        processor=rec,
        session_factory=session_factory,
        settings=settings,
        offset=offset,
    )
    assert offset2 == 503


async def test_run_polling_loop(session_factory) -> None:
    class FakeSource:
        def __init__(self, stop: asyncio.Event) -> None:
            self.stop = stop
            self.offsets: list[int] = []

        async def get_updates(self, offset: int):
            self.offsets.append(offset)
            if len(self.offsets) == 1:
                return [make_update(41), make_update(42)]
            self.stop.set()
            return []

        async def delete_webhook(self) -> None:
            self.deleted = True

    await dedup.reserve(session_factory, 41)  # simulates previously processed batch
    stop = asyncio.Event()
    rec = Recorder()
    source = FakeSource(stop)
    await asyncio.wait_for(
        run_polling(
            source=source,
            processor=rec,
            session_factory=session_factory,
            settings=settings,
            stop=stop,
            idle_seconds=0.01,
        ),
        timeout=5,
    )
    assert source.deleted is True
    assert source.offsets[0] == 42  # resumed from last processed id
    assert rec.seen == [42]  # 41 was deduped away
