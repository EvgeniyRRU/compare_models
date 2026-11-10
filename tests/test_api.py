from types import SimpleNamespace

import httpx

from app.db.models import User
from app.main import app
from tests.test_auth import signed


async def test_api_requires_signature_and_database_role(sessions):
    import time

    app.state.sessions = sessions
    app.state.settings = SimpleNamespace(bot_token="token", init_data_max_age_seconds=60)
    async with sessions.begin() as session:
        session.add(User(telegram_id=123, name="One", role="support"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/me")).status_code == 401
        headers = {"Authorization": "tma " + signed("token", int(time.time()))}
        response = await client.get("/api/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["role"] == "support"
        assert (await client.get("/api/users", headers=headers)).status_code == 403
        async with sessions.begin() as session:
            user = await session.get(User, response.json()["id"])
            user.role = None
        assert (await client.get("/api/me", headers=headers)).status_code == 403
