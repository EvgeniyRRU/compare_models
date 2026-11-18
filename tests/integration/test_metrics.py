# tests/integration/test_metrics.py
"""Сентри, HTTP-метрики, metrics-порт."""

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
