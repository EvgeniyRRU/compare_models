import httpx
import pytest

from app.main import create_app


@pytest.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_live_ok(client) -> None:
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_ready_ok(client) -> None:
    r = await client.get("/health/ready")
    assert r.status_code == 200


async def test_ready_db_down(client) -> None:
    from app.db import session as db_session

    original = db_session.engine

    class _BrokenEngine:
        def connect(self):
            raise RuntimeError("cannot connect")

    db_session.engine = _BrokenEngine()  # type: ignore[assignment]
    try:
        r = await client.get("/health/ready")
        assert r.status_code == 503
        assert r.json() == {"status": "db_unavailable"}
    finally:
        db_session.engine = original
