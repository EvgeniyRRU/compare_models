"""API requests use signed Telegram initData and actual PostgreSQL roles/state."""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from support_bot.api.health import router as health_router
from support_bot.api.tickets import router as ticket_router
from support_bot.api.users import router as user_router
from support_bot.db.models import User
from support_bot.domain.errors import BusinessError
from support_bot.services.tickets import TicketService
from support_bot.services.users import UserService

pytestmark = pytest.mark.integration


def authorization(settings, telegram_id):
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": telegram_id, "first_name": "API user"}),
    }
    secret = hmac.new(
        b"WebAppData", settings.bot_token.get_secret_value().encode(), hashlib.sha256
    ).digest()
    fields["hash"] = hmac.new(
        secret, "\n".join(f"{k}={v}" for k, v in sorted(fields.items())).encode(), hashlib.sha256
    ).hexdigest()
    return {"Authorization": "tma " + urlencode(fields)}


@pytest.fixture
async def api(db, settings):
    app = FastAPI()
    users, tickets = UserService(db.sessions, settings), TicketService(db.sessions, settings)
    app.state.settings, app.state.db = settings, db
    app.state.users, app.state.tickets = users, tickets
    app.state.posts = SimpleNamespace(sync=AsyncMock())
    for router in (ticket_router, user_router, health_router):
        app.include_router(router)

    @app.exception_handler(BusinessError)
    async def error(request, exc):
        status = {
            "forbidden": 403,
            "conflict": 409,
            "not_found": 404,
            "post_restore_failed": 503,
        }.get(exc.code, 400)
        return JSONResponse(
            {"code": exc.code, "message": exc.message, "ticket_ids": exc.ticket_ids},
            status_code=status,
        )

    admin = await users.touch(101, "Admin")
    support = await users.touch(102, "Support")
    developer = await users.touch(103, "Developer")
    async with db.sessions() as session, session.begin():
        for person, role in ((admin, "admin"), (support, "support"), (developer, "developer")):
            await session.execute(update(User).where(User.id == person.id).values(role=role))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            app=app,
            users=users,
            tickets=tickets,
            admin=admin,
            support=support,
            developer=developer,
            settings=settings,
        )


async def test_api_auth_current_role_and_revocation(api, db):
    client, headers = api.client, authorization(api.settings, 102)
    assert (await client.get("/api/me")).status_code == 401
    assert (
        await client.get("/api/me", headers={"Authorization": "tma invalid"})
    ).status_code == 401
    assert (await client.get("/api/me", headers=headers)).json()["role"] == "support"
    assert (await client.get("/api/users", headers=headers)).status_code == 403
    async with db.sessions() as session, session.begin():
        await session.execute(update(User).where(User.id == api.support.id).values(role=None))
    assert (await client.get("/api/me", headers=headers)).status_code == 403


async def test_ticket_filters_details_and_pagination(api):
    a = await api.tickets.create(
        actor_id=api.support.id,
        description="full <description>",
        priority="high",
        request_id="REQ",
        tenant_id="TEN",
    )
    await api.tickets.create(
        actor_id=api.admin.id, description="other", priority="low", request_id="REQ2"
    )
    headers = authorization(api.settings, 102)
    response = await api.client.get(
        "/api/tickets?scope=created&priority=high&page_size=1", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == a.id
    detail = (await api.client.get(f"/api/tickets/{a.id}", headers=headers)).json()
    assert detail["description"] == "full <description>"
    assert detail["author"]["id"] == api.support.id
    assert detail["tenant_id"] == "TEN"
    for query in ("page=0", "page_size=101", "scope=unknown", "status=bad", "priority=bad"):
        assert (await api.client.get("/api/tickets?" + query, headers=headers)).status_code == 422
    assert (await api.client.get("/api/tickets/999999", headers=headers)).status_code == 404


async def test_admin_mutations_conflict_and_restore_preserve_version(api):
    ticket = await api.tickets.create(
        actor_id=api.support.id, description="test", priority="medium", request_id="1"
    )
    ticket = await api.tickets.change(
        actor_id=api.developer.id,
        ticket_id=ticket.id,
        expected_version=ticket.version,
        expected_status=ticket.status,
        action="take",
    )
    headers = authorization(api.settings, 101)
    data = {
        "expected_version": ticket.version,
        "expected_status": ticket.status,
        "assignee_id": api.admin.id,
    }
    assert (
        await api.client.post(
            f"/api/tickets/{ticket.id}/reassign",
            headers=authorization(api.settings, 102),
            json=data,
        )
    ).status_code == 403
    result = await api.client.post(f"/api/tickets/{ticket.id}/reassign", headers=headers, json=data)
    assert result.status_code == 200
    assert result.json()["version"] == ticket.version + 1
    stale = await api.client.post(f"/api/tickets/{ticket.id}/reassign", headers=headers, json=data)
    assert stale.status_code == 409
    restore = await api.client.post(f"/api/tickets/{ticket.id}/restore-post", headers=headers)
    assert restore.status_code == 200
    assert restore.json()["version"] == ticket.version + 1
    api.app.state.posts.sync.assert_awaited_with(ticket.id, force=True)


async def test_admin_role_list_nullable_assignment_and_eligible_filter(api):
    headers = authorization(api.settings, 101)
    users = (await api.client.get("/api/users?eligible=true", headers=headers)).json()
    assert {u["id"] for u in users["items"]} == {api.admin.id, api.developer.id}
    result = await api.client.patch(
        f"/api/users/{api.support.id}/role", headers=headers, json={"role": None}
    )
    assert result.status_code == 200
    assert result.json()["role"] is None
    assert (await api.client.get("/api/users?page_size=101", headers=headers)).status_code == 422


async def test_health_readiness_uses_database(api):
    assert (await api.client.get("/health/live")).status_code == 200
    assert (await api.client.get("/health/ready")).status_code == 200


async def test_restore_failure_is_visible_for_clean_post(api):
    ticket = await api.tickets.create(
        actor_id=api.support.id, description="test", priority="medium", request_id="1"
    )
    api.app.state.posts.sync.side_effect = RuntimeError("Telegram unavailable")
    result = await api.client.post(
        f"/api/tickets/{ticket.id}/restore-post", headers=authorization(api.settings, 101)
    )
    assert result.status_code == 503
    assert result.json()["code"] == "post_restore_failed"


async def test_restore_lock_timeout_is_visible(api):
    ticket = await api.tickets.create(
        actor_id=api.support.id, description="test", priority="medium", request_id="1"
    )
    api.app.state.posts.sync.return_value = False
    result = await api.client.post(
        f"/api/tickets/{ticket.id}/restore-post", headers=authorization(api.settings, 101)
    )
    assert result.status_code == 503
    assert result.json()["code"] == "post_restore_failed"
