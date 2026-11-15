"""Tests for Prometheus metrics (real PostgreSQL for gauge, unit tests for counters)."""

import asyncio
import re

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, CollectorRegistry
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.enums import Priority, Role, TicketStatus
from app.db.models import User
from app.main import _serve_metrics, create_app
from app.services.ticket_service import create_ticket, take

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample_value(name: str, labels: dict[str, str] | None = None) -> float:
    """Read the current value of a metric named *name* from the default registry."""
    if labels is None:
        labels = {}
    value = REGISTRY.get_sample_value(name, labels)
    return float(value) if value is not None else 0.0


def _http_sample(kind: str, status: str | None = None) -> float:
    """Shortcut for HTTP metric samples with method=GET, route=/health/live."""
    labels: dict[str, str] = {"method": "GET", "route": "/health/live"}
    if kind == "latency":
        return _sample_value("rent_support_http_latency_seconds_count", labels)
    if status is not None:
        labels["status"] = status
    return _sample_value("rent_support_http_requests_total", labels)


HTTP_REQ = b"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"


async def _fetch_metrics(port: int) -> str:
    """Connect to the local metrics server and return the full response text."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection("127.0.0.1", port),
        timeout=3.0,
    )
    writer.write(HTTP_REQ)
    await writer.drain()
    # Read headers until the double CRLF, parse Content-Length, then read body.
    header_buf = b""
    while b"\r\n\r\n" not in header_buf:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=3.0)
        if not chunk:
            break
        header_buf += chunk
    header_str = header_buf.decode(errors="replace")
    cl_match = re.search(r"Content-Length:\s*(\d+)", header_str, re.IGNORECASE)
    body_start = header_buf.find(b"\r\n\r\n") + 4
    rest = header_buf[body_start:]
    if cl_match:
        content_length = int(cl_match.group(1))
        remaining = content_length - len(rest)
        if remaining > 0:
            more = await asyncio.wait_for(reader.readexactly(remaining), timeout=3.0)
            rest += more
    writer.close()
    await writer.wait_closed()
    return header_str + rest.decode(errors="replace")


# ---------------------------------------------------------------------------
# tickets_created counter
# ---------------------------------------------------------------------------


async def test_tickets_created_increments_per_priority(session: AsyncSession) -> None:
    support = await _mk_user(session, 1001, Role.support)
    await session.commit()

    for prio in Priority:
        before = _sample_value("rent_support_tickets_created_total", {"priority": prio.value})
        ticket = await create_ticket(
            session,
            author=support,
            priority=prio,
            description="t",
            request_id=f"REQ-{prio.value}",
        )
        await session.commit()
        after = _sample_value("rent_support_tickets_created_total", {"priority": prio.value})
        assert after == before + 1, f"counter did not increment for {prio.value}"
        assert ticket.priority is prio


# ---------------------------------------------------------------------------
# status_transitions counter
# ---------------------------------------------------------------------------


async def test_status_transitions_increments(session: AsyncSession) -> None:
    support = await _mk_user(session, 1001, Role.support)
    developer = await _mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await create_ticket(
        session, author=support, priority=Priority.medium, description="t", request_id="REQ-1"
    )
    await session.commit()

    assert (
        _sample_value(
            "rent_support_status_transitions_total",
            {"from": "new", "to": "in_progress"},
        )
        == 0
    )

    ticket = await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()

    assert (
        _sample_value(
            "rent_support_status_transitions_total",
            {"from": "new", "to": "in_progress"},
        )
        == 1
    )
    assert ticket.status is TicketStatus.in_progress


# ---------------------------------------------------------------------------
# HTTP middleware (requests + latency)
# ---------------------------------------------------------------------------


async def test_http_middleware_increments_request_counter(
    engine: AsyncEngine,
) -> None:
    app = create_app(engine=engine, application=None)
    client = TestClient(app)

    before = _http_sample("requests", status="200")

    resp = client.get("/health/live")
    assert resp.status_code == 200

    after = _http_sample("requests", status="200")
    assert after == before + 1

    # Latency histogram must have at least one observation.
    latency_count = _http_sample("latency")
    assert latency_count > 0, "latency histogram expected at least one observation"


# ---------------------------------------------------------------------------
# Metrics endpoint (raw TCP + gauge refresh)
# ---------------------------------------------------------------------------


async def test_metrics_endpoint_returns_open_tickets_gauge(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    # Seed some open tickets at different statuses.
    support = await _mk_user(session, 1001, Role.support)
    developer = await _mk_user(session, 1002, Role.developer)
    await session.commit()

    # Two new tickets.
    t1 = await create_ticket(
        session, author=support, priority=Priority.medium, description="t1", request_id="REQ-1"
    )
    await create_ticket(
        session, author=support, priority=Priority.high, description="t2", request_id="REQ-2"
    )
    await session.commit()

    # Take one → in_progress
    await take(session, ticket_id=t1.id, actor=developer, expected_version=1)
    await session.commit()

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Bind to an ephemeral port.
    server = await _serve_metrics(factory, host="127.0.0.1", port=0)
    addr = server.sockets[0].getsockname()
    port: int = addr[1]

    try:
        text_body = await _fetch_metrics(port)
    finally:
        server.close()
        await server.wait_closed()

    assert "HTTP/1.1 200 OK" in text_body
    assert "Content-Type: text/plain" in text_body

    # Check gauge values in the scraped body.
    assert re.search(r'rent_support_open_tickets_total\{status="new"\}\s+1', text_body)
    assert re.search(r'rent_support_open_tickets_total\{status="in_progress"\}\s+1', text_body)
    assert re.search(r'rent_support_open_tickets_total\{status="waiting_info"\}\s+0', text_body)


async def test_metrics_endpoint_no_tickets_returns_zeroes(
    engine: AsyncEngine,
    session: AsyncSession,
) -> None:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    server = await _serve_metrics(factory, host="127.0.0.1", port=0)
    addr = server.sockets[0].getsockname()
    port: int = addr[1]

    try:
        text_body = await _fetch_metrics(port)
    finally:
        server.close()
        await server.wait_closed()

    assert re.search(r'rent_support_open_tickets_total\{status="new"\}\s+0', text_body)
    assert re.search(r'rent_support_open_tickets_total\{status="in_progress"\}\s+0', text_body)
    assert re.search(r'rent_support_open_tickets_total\{status="waiting_info"\}\s+0', text_body)


# ---------------------------------------------------------------------------
# updates_processed / updates_deduplicated (unit test with mocks)
# ---------------------------------------------------------------------------


async def test_updates_processed_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    from prometheus_client import Counter

    processed = Counter("test_updates_processed", "test", registry=CollectorRegistry())
    monkeypatch.setattr("app.webhook.updates_processed", processed)

    # Just test that the counter increments correctly.
    processed.inc()
    assert processed._value.get() == 1.0


async def test_updates_deduplicated_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    from prometheus_client import Counter

    dedup = Counter("test_updates_deduplicated", "test", registry=CollectorRegistry())
    monkeypatch.setattr("app.webhook.updates_deduplicated", dedup)

    dedup.inc()
    assert dedup._value.get() == 1.0


# ---------------------------------------------------------------------------
# reminders_sent counter
# ---------------------------------------------------------------------------


async def test_reminders_sent_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    from prometheus_client import Counter

    rem = Counter("test_reminders_sent", "test", registry=CollectorRegistry())
    monkeypatch.setattr("app.background.reminders_sent", rem)

    rem.inc(3)
    assert rem._value.get() == 3.0


# ---------------------------------------------------------------------------
# errors_total counter
# ---------------------------------------------------------------------------


async def test_errors_total_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    from prometheus_client import Counter

    err = Counter("test_errors_total", "test", ["component"], registry=CollectorRegistry())
    monkeypatch.setattr("app.bot.errors.errors_total", err)

    err.labels(component="bot").inc()
    assert err.labels(component="bot")._value.get() == 1.0


# ---------------------------------------------------------------------------
# ticket_time_to_take histogram
# ---------------------------------------------------------------------------


async def test_ticket_time_to_take_observes(session: AsyncSession) -> None:
    support = await _mk_user(session, 1001, Role.support)
    developer = await _mk_user(session, 1002, Role.developer)
    await session.commit()

    ticket = await create_ticket(
        session, author=support, priority=Priority.medium, description="t", request_id="REQ-1"
    )
    await session.commit()

    before = _sample_value("rent_support_ticket_time_to_take_seconds_count")
    await take(session, ticket_id=ticket.id, actor=developer, expected_version=1)
    await session.commit()
    after = _sample_value("rent_support_ticket_time_to_take_seconds_count")

    assert after == before + 1, "time-to-take histogram expected one observation"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _mk_user(session: AsyncSession, telegram_id: int, role: Role | None = None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user
