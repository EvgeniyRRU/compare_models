from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from support_bot.db.enums import TicketPriority, UserRole
from support_bot.main import create_metrics_app
from support_bot.observability.context import RequestContextMiddleware
from support_bot.observability.metrics import (
    HTTP_REQUESTS,
    refresh_open_tickets,
    render_metrics,
)


async def test_http_middleware_uses_route_template() -> None:
    from support_bot.observability.metrics import PrometheusMiddleware

    app = FastAPI()
    app.add_middleware(PrometheusMiddleware)

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    before = HTTP_REQUESTS.labels(method="GET", route="/items/{item_id}", status="200")._value.get()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/items/42")
    assert response.status_code == 200
    after = HTTP_REQUESTS.labels(method="GET", route="/items/{item_id}", status="200")._value.get()
    assert after == before + 1

    payload = render_metrics().decode()
    assert 'route="/items/{item_id}"' in payload
    assert 'route="/items/42"' not in payload


async def test_request_context_middleware_sets_request_id() -> None:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        provided = await client.get("/ping", headers={"X-Request-ID": "abc-123"})
        generated = await client.get("/ping")

    assert provided.headers["X-Request-ID"] == "abc-123"
    assert generated.headers["X-Request-ID"]


async def test_open_tickets_gauge_is_refreshed_at_scrape(
    session_factory, make_user, make_ticket
) -> None:
    author = await make_user(telegram_id=600, role=UserRole.SUPPORT)
    await make_ticket(author_id=author.id, priority=TicketPriority.LOW, request_id="LOW-1")
    await make_ticket(author_id=author.id, priority=TicketPriority.HIGH, request_id="HIGH-1")

    async with session_factory() as session:
        counts = await refresh_open_tickets(session)

    assert sum(counts.values()) >= 2
    payload = render_metrics().decode()
    assert "support_bot_open_tickets{" in payload
    assert 'status="new"' in payload


async def test_metrics_app_serves_scrape_endpoint(session_factory) -> None:
    app = create_metrics_app(session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "support_bot_open_tickets" in response.text
