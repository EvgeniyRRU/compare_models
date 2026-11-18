# tests/integration/test_metrics.py
"""Сентри, HTTP-метрики, metrics-порт."""

import asyncio

import httpx
import sentry_sdk
from telegram import Update

from tests.fakes import make_update


async def test_ptb_error_handler_sends_to_sentry(tg_app, bot, monkeypatch):
    calls: list = []
    monkeypatch.setattr(sentry_sdk, "capture_exception", lambda exc: calls.append(exc))
    from app.metrics import errors_total

    before = _counter(errors_total, {"component": "telegram"})
    bot.fail_next_send = RuntimeError("telegram down")
    d = make_update(1, 111, text="/start")
    await tg_app.process_update(Update.de_json(d, tg_app.bot))
    assert calls and isinstance(calls[0], RuntimeError)
    assert _counter(errors_total, {"component": "telegram"}) == before + 1


def _counter(metric, labels: dict) -> float:
    for m in metric.collect():
        for s in m.samples:
            if s.labels == labels and s.name.endswith("_total"):
                return s.value
    return 0.0


def test_http_metrics_path_template(db, settings):
    from fastapi import FastAPI

    from app.api.health import router as health_router
    from app.metrics import add_http_metrics, http_requests_total

    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    app.include_router(health_router)
    add_http_metrics(app)
    transport = httpx.ASGITransport(app=app)

    async def run():
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/health/live")).status_code == 200
            assert (await c.get("/health/live")).status_code == 200
            assert (await c.get("/no-such-path")).status_code == 404

    asyncio.run(run())

    def value(labels: dict) -> float:
        for m in http_requests_total.collect():
            for s in m.samples:
                if s.labels == labels and s.name.endswith("_total"):
                    return s.value
        return 0.0

    assert value({"method": "GET", "route": "/health/live", "status": "200"}) >= 2
    assert value({"method": "GET", "route": "unmatched", "status": "404"}) >= 1


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_metrics_port_and_gauge(db, settings):
    from app.metrics import start_metrics_server
    from app.services import tickets as tickets_svc
    from app.services import users as users_svc

    # два обращения: new и in_progress
    async with db.session_factory() as s, s.begin():
        u = await users_svc.upsert_user(s, telegram_id=1)
        d = await users_svc.upsert_user(s, telegram_id=2)
        await users_svc.change_role(s, user_id=d.id, new_role="developer")
        await tickets_svc.create_ticket(
            s, settings, u.id, description="d", priority="high", request_id="r1"
        )
        t2 = await tickets_svc.create_ticket(
            s, settings, u.id, description="d2", priority="low", request_id="r2"
        )
        await tickets_svc.take_ticket(s, settings, d.id, t2, expected_version=1)

    port = _free_port()
    server, task = start_metrics_server(port, db)
    try:
        async with httpx.AsyncClient() as client:
            r = None
            for _ in range(100):
                try:
                    r = await client.get(f"http://127.0.0.1:{port}/metrics", timeout=0.5)
                    break
                except Exception:
                    await asyncio.sleep(0.1)
            assert r is not None and r.status_code == 200
            text = r.text
            assert 'tickets_open_by_status{status="new"} 1.0' in text
            assert 'tickets_open_by_status{status="in_progress"} 1.0' in text
            assert 'tickets_open_by_status{status="resolved"} 0.0' in text
            assert "ticket_take_duration_seconds" in text
    finally:
        server.should_exit = True
        await task


async def test_metrics_not_on_main_port(db, settings):
    """Основной HTTP-порт не отдаёт /metrics (отдельный METRICS_PORT)."""
    from app.config import Settings
    from app.main import create_app
    from tests.conftest import FAKE_TOKEN, TEST_DB_URL

    s = Settings(bot_token=FAKE_TOKEN, dev_chat_id=-1, database_url=TEST_DB_URL, _env_file=None)
    app = create_app(s)
    assert "/metrics" not in [getattr(r, "path", "") for r in app.routes]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/metrics")
        assert r.status_code == 404
