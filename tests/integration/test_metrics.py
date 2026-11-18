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
