# tests/integration/test_health.py
import httpx
import pytest_asyncio
from fastapi import FastAPI

from app.api.health import router as health_router


@pytest_asyncio.fixture
async def client(db, settings):
    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    app.include_router(health_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_live(client):
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_ok(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}
