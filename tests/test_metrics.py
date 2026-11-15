"""Monitoring: route-template HTTP metrics, DB gauge, standalone /metrics app."""

from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY

from app.db.models import Role
from app.metrics import create_metrics_app, refresh_open_tickets
from tests.test_api_tickets import add_user, hdr, mk_ticket, take

SUPPORT = 850
DEV = 851


def _value(name: str, **labels: str) -> float:
    """Read one sample from the global registry; unexposed label sets count as 0.

    Callers pass the full sample name ("http_requests_total", "open_tickets_by_status",
    or "http_request_duration_seconds_sum" for histograms) because bucket/created
    samples share labels and must not be mistaken for the target one.
    """
    family = name.removesuffix("_total")
    for metric in REGISTRY.collect():
        if metric.name not in (family, name):
            continue
        for sample in metric.samples:
            if sample.name != name:
                continue
            if all(sample.labels.get(k) == v for k, v in labels.items()):
                return float(sample.value)
    return 0.0


async def test_middleware_uses_route_template(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    labels = {"method": "GET", "route": "/api/tickets/{ticket_id}", "status": "404"}
    before = _value("http_requests_total", **labels)
    r = await api_client.get("/api/tickets/424242", headers=hdr(SUPPORT))
    assert r.status_code == 404
    assert _value("http_requests_total", **labels) == before + 1.0


async def test_middleware_counts_list_route(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    await mk_ticket(session_factory, author=SUPPORT)
    labels = {"method": "GET", "route": "/api/tickets", "status": "200"}
    before = _value("http_requests_total", **labels)
    r = await api_client.get("/api/tickets?scope=mine", headers=hdr(SUPPORT))
    assert r.status_code == 200
    assert _value("http_requests_total", **labels) == before + 1.0


async def test_business_counter_through_service(session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    prio = "critical"
    before = _value("tickets_created_total", priority=prio)
    await mk_ticket(session_factory, author=SUPPORT, priority=prio)
    assert _value("tickets_created_total", priority=prio) == before + 1.0


async def test_gauge_drops_when_ticket_closes(session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    await add_user(session_factory, DEV, Role.developer)
    tid = await mk_ticket(session_factory, author=SUPPORT)
    await mk_ticket(session_factory, author=SUPPORT, rid="RQ-2")  # stays 'new'
    await take(session_factory, tid, DEV)
    await refresh_open_tickets(session_factory)
    assert _value("open_tickets_by_status", status="new") == 1.0
    assert _value("open_tickets_by_status", status="in_progress") == 1.0

    from app.config import settings
    from app.domain.state_machine import Action
    from app.services import tickets as tickets_svc

    async with session_factory() as s, s.begin():
        await tickets_svc.apply_action(
            s, settings, ticket_id=tid, actor_id=DEV, action=Action.resolve, expected_version=2
        )
    await refresh_open_tickets(session_factory)
    assert _value("open_tickets_by_status", status="in_progress") == 0.0


async def test_metrics_app_serves_registry() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=create_metrics_app()), base_url="http://metrics"
    ) as c:
        r = await c.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        assert "app_errors_total" in r.text or "app_errors" in r.text
        assert "tickets_created_total" in r.text
        assert (await c.get("/healthz")).text == "ok"
        assert (await c.get("/nope")).status_code == 404
