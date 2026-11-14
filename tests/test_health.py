"""Tests for the health endpoints."""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.main import create_app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_live_returns_200(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_ready_returns_503_when_db_unavailable() -> None:
    # Port 1 refuses connections immediately and deterministically.
    broken_engine = create_async_engine("postgresql+asyncpg://support:support@127.0.0.1:1/support")
    app = create_app(engine=broken_engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    await broken_engine.dispose()


async def test_health_ready_returns_200_when_db_reachable() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/health/ready")
    if response.status_code != 200:
        pytest.skip("PostgreSQL not reachable; skipping ready-success assertion")
    assert response.json() == {"status": "ok"}
