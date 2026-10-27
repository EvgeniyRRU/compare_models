import json
import logging

from structlog.contextvars import bind_contextvars, clear_contextvars


def test_json_logging_preserves_context(capsys, monkeypatch):
    from support_bot.observability.logging import configure_logging

    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", root.handlers[:])
    monkeypatch.setattr(root, "level", root.level)
    configure_logging("INFO", "json", ("secret-token",))
    bind_contextvars(request_id="r1", ticket_id=42, component="test")
    logging.getLogger("third.party").warning("failure secret-token")
    data = json.loads(capsys.readouterr().out)
    assert data["request_id"] == "r1"
    assert data["ticket_id"] == 42
    assert data["component"] == "test"
    assert data["event"] == "failure [REDACTED]"
    try:
        raise RuntimeError("diagnostic secret-token")
    except RuntimeError as error:
        from support_bot.observability.sentry import capture_error

        capture_error(error, component="test")
    diagnostic = json.loads(capsys.readouterr().out)
    assert "Traceback" in diagnostic["exception"]
    assert "diagnostic [REDACTED]" in diagnostic["exception"]
    assert "secret-token" not in diagnostic["exception"]
    clear_contextvars()


def test_sentry_without_dsn_is_disabled(settings):
    from support_bot.observability.sentry import configure_sentry

    assert configure_sentry(settings) is None


async def test_http_metrics_use_template_path():
    import httpx
    from fastapi import FastAPI
    from prometheus_client import generate_latest

    from support_bot.observability.metrics import HTTPMetricsMiddleware, Metrics

    metrics = Metrics()
    app = FastAPI()
    app.add_middleware(HTTPMetricsMiddleware, metrics=metrics)

    @app.get("/tickets/{ticket_id}")
    async def card(ticket_id: int):
        return {"id": ticket_id}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get("/tickets/123")
        await client.get("/tickets/456")
        await client.get("/unknown/1")
    output = generate_latest(metrics.registry).decode()
    assert 'route="/tickets/{ticket_id}"' in output
    assert 'route="/tickets/123"' not in output
    assert 'route="unmatched"' in output
