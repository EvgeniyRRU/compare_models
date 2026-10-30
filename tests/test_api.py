import hashlib
import hmac
import json
import time
from datetime import timedelta
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api.auth import InvalidInitData, validate_init_data
from app.api.routes import router
from app.bot.context import AppContext
from app.config import Settings
from app.db.models import Ticket, User
from app.domain.state_machine import Action, Priority
from app.services.drafts import DraftService
from app.services.post_sync import PostSyncer
from app.services.tickets import NewTicket
from tests.fakes import FakePublisher

TOKEN = "123:TEST"


def init_data(user_id: int, token: str = TOKEN, auth_date: int | None = None, **extra) -> str:
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAA",
        "user": json.dumps({"id": user_id, "first_name": "N", "username": f"u{user_id}"}),
        **extra,
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_init_data_valid():
    ident = validate_init_data(init_data(42), TOKEN, 3600)
    assert ident.id == 42 and ident.username == "u42"


def test_init_data_tampered_or_wrong_token():
    good = init_data(42)
    with pytest.raises(InvalidInitData):
        validate_init_data(good.replace("u42", "u43"), TOKEN, 3600)
    with pytest.raises(InvalidInitData):
        validate_init_data(init_data(42, token="999:OTHER"), TOKEN, 3600)
    with pytest.raises(InvalidInitData):
        validate_init_data("auth_date=1&user=%7B%7D", TOKEN, 3600)


def test_init_data_expired():
    with pytest.raises(InvalidInitData):
        validate_init_data(init_data(42, auth_date=int(time.time()) - 7200), TOKEN, 3600)


@pytest.fixture
async def client(engine, sm, svc):
    pub = FakePublisher()
    settings = Settings(bot_token=TOKEN, dev_chat_id=-1, bootstrap_admin_ids="999")
    ctx = AppContext(
        settings=settings,
        engine=engine,
        sm=sm,
        tickets=svc,
        drafts=DraftService(svc, timedelta(hours=24)),
        syncer=PostSyncer(engine, sm, pub, -1, 3),
        publisher=pub,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.ctx = ctx
    app.state.pub = pub
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        c.pub = pub
        yield c


def auth(tg_id: int) -> dict[str, str]:
    return {"Authorization": "tma " + init_data(tg_id)}


async def new_ticket(sm, svc, users, desc="d") -> Ticket:
    async with sm() as s, s.begin():
        return await svc.create(s, users["support"].id, NewTicket(desc, Priority.HIGH, "R"))


async def test_requires_valid_auth(client, users):
    assert (await client.get("/api/me")).status_code == 401
    assert (
        await client.get("/api/me", headers={"Authorization": "tma garbage"})
    ).status_code == 401


async def test_user_without_role_forbidden_and_registered(client, sm):
    r = await client.get("/api/me", headers=auth(777))
    assert r.status_code == 403
    async with sm() as s:
        assert (
            await s.execute(select(User).where(User.telegram_id == 777))
        ).scalar_one().role is None


async def test_bootstrap_admin_gets_role(client):
    r = await client.get("/api/me", headers=auth(999))
    assert r.status_code == 200 and r.json()["role"] == "admin"


async def test_role_change_applies_immediately(client, users):
    assert (await client.get("/api/me", headers=auth(5))).status_code == 403
    r = await client.put(
        f"/api/users/{users['nobody'].id}/role", json={"role": "support"}, headers=auth(4)
    )
    assert r.status_code == 200
    assert (await client.get("/api/me", headers=auth(5))).json()["role"] == "support"


async def test_roles_endpoints_admin_only(client, users):
    assert (await client.get("/api/users", headers=auth(1))).status_code == 403
    assert (await client.get("/api/users", headers=auth(2))).status_code == 403
    r = await client.get("/api/users?assignable=true", headers=auth(4))
    assert {u["role"] for u in r.json()} == {"developer", "admin"}


async def test_role_downgrade_blocked_returns_ticket_list(client, sm, svc, users):
    t = await new_ticket(sm, svc, users)
    async with sm() as s, s.begin():
        await svc.apply(s, t.id, Action.TAKE, users["dev"].id, 1)
    r = await client.put(
        f"/api/users/{users['dev'].id}/role", json={"role": "support"}, headers=auth(4)
    )
    assert r.status_code == 409
    assert r.json()["detail"]["ticket_ids"] == [t.id]


async def test_list_filters_pagination_scopes(client, sm, svc, users):
    ids = [(await new_ticket(sm, svc, users, f"d{i}")).id for i in range(5)]
    async with sm() as s, s.begin():
        await svc.apply(s, ids[0], Action.TAKE, users["dev"].id, 1)
    r = await client.get("/api/tickets?page_size=2&page=1", headers=auth(1))
    body = r.json()
    assert body["total"] == 5 and [t["id"] for t in body["items"]] == [5, 4]  # новые сверху
    assert [
        t["id"]
        for t in (await client.get("/api/tickets?page_size=2&page=3", headers=auth(1))).json()[
            "items"
        ]
    ] == [1]
    assert (await client.get("/api/tickets?status=in_progress", headers=auth(1))).json()[
        "total"
    ] == 1
    assert (await client.get("/api/tickets?priority=low", headers=auth(1))).json()["total"] == 0
    assert (await client.get("/api/tickets?scope=assigned", headers=auth(2))).json()["total"] == 1
    assert (await client.get("/api/tickets?scope=assigned", headers=auth(3))).json()["total"] == 0
    assert (await client.get("/api/tickets?scope=created", headers=auth(1))).json()["total"] == 5
    assert (await client.get("/api/tickets?scope=created", headers=auth(2))).json()["total"] == 0


async def test_ticket_card_full_description(client, sm, svc, users):
    t = await new_ticket(sm, svc, users, "x" * 3000)
    r = await client.get(f"/api/tickets/{t.id}", headers=auth(2))
    assert len(r.json()["description"]) == 3000
    assert (await client.get("/api/tickets/9999", headers=auth(2))).status_code == 404


async def test_reopen_reassign_admin_only_with_version(client, sm, svc, users):
    t = await new_ticket(sm, svc, users)
    async with sm() as s, s.begin():
        await svc.apply(s, t.id, Action.TAKE, users["dev"].id, 1)
    body = {"version": 2, "assignee_id": users["dev2"].id}
    assert (
        await client.post(f"/api/tickets/{t.id}/reassign", json=body, headers=auth(2))
    ).status_code == 403
    stale = await client.post(
        f"/api/tickets/{t.id}/reassign", json={**body, "version": 1}, headers=auth(4)
    )
    assert stale.status_code == 409
    bad_target = await client.post(
        f"/api/tickets/{t.id}/reassign",
        json={**body, "assignee_id": users["support"].id},
        headers=auth(4),
    )
    assert bad_target.status_code == 422
    ok = await client.post(f"/api/tickets/{t.id}/reassign", json=body, headers=auth(4))
    assert ok.status_code == 200 and ok.json()["assignee"]["id"] == users["dev2"].id
    assert ok.json()["version"] == 3
    assert len(client.pub.sent) == 1  # после успеха пост синхронизирован

    async with sm() as s, s.begin():
        await svc.apply(s, t.id, Action.RESOLVE, users["dev2"].id, 3)
    assert (
        await client.post(f"/api/tickets/{t.id}/reopen", json={"version": 4}, headers=auth(3))
    ).status_code == 403
    r = await client.post(f"/api/tickets/{t.id}/reopen", json={"version": 4}, headers=auth(4))
    assert r.json()["status"] == "new"


async def test_restore_post_keeps_version(client, sm, svc, users):
    t = await new_ticket(sm, svc, users)
    assert (
        await client.post(f"/api/tickets/{t.id}/restore-post", headers=auth(1))
    ).status_code == 403
    r = await client.post(f"/api/tickets/{t.id}/restore-post", headers=auth(4))
    assert r.status_code == 200 and r.json()["version"] == 1
    assert len(client.pub.sent) == 1
