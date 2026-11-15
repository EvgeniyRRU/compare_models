"""Tests that the Mini App static frontend is mounted at the root."""

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app.main import create_app


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(engine=engine))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_root_serves_index_html(client: httpx.AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "telegram-web-app.js" in response.text


async def test_static_assets_served(client: httpx.AsyncClient) -> None:
    js = await client.get("/app.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]

    css = await client.get("/styles.css")
    assert css.status_code == 200
    assert "css" in css.headers["content-type"]


async def test_api_still_takes_precedence_over_static(client: httpx.AsyncClient) -> None:
    # A route that exists as a static file must not shadow the API; the API
    # router is mounted before the static mount, so /api/* still answers JSON.
    response = await client.get("/api/tickets")

    assert response.status_code == 401  # missing initData, not the static 404


async def test_health_still_takes_precedence_over_static(client: httpx.AsyncClient) -> None:
    # The "/" static mount is registered last, so the /health routes must still
    # match rather than falling through to the static file server.
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
