# tests/integration/test_api.py
"""REST API Mini App."""

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select, text

from app.api.tickets import router as tickets_router
from app.api.users import router as users_router
from app.models import User
from app.services import tickets as tickets_svc
from app.services import users as users_svc
from tests.fakes import make_init_data


@pytest_asyncio.fixture
async def api_client(db, settings, tg_app):
    app = FastAPI()
    app.state.settings = settings
    app.state.db = db
    app.state.tg_app = tg_app
    app.include_router(tickets_router, prefix="/api")
    app.include_router(users_router, prefix="/api")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _auth(user_id: int, settings, **kw) -> dict:
    return {"initData": make_init_data(user_id, settings.bot_token, **kw)}


async def _setup_roles(db, settings):
    """admin=111, support=222, dev=333, dev2=444."""
    async with db.session_factory() as s, s.begin():
        roles = {111: "admin", 222: "support", 333: "developer", 444: "developer"}
        ids = {}
        for tg, role in roles.items():
            u = await users_svc.upsert_user(s, telegram_id=tg, first_name=f"U{tg}")
            await users_svc.change_role(s, user_id=u.id, new_role=role)
            ids[tg] = u.id
    return ids


async def _make_ticket(
    db, settings, author_tg: int, *, priority="high", rid="API-1", tenant=None
) -> int:
    async with db.session_factory() as s, s.begin():
        u = (await s.execute(select(User).where(User.telegram_id == author_tg))).scalar_one()
        t = await tickets_svc.create_ticket(
            s,
            settings,
            u.id,
            description="тест",
            priority=priority,
            request_id=rid,
            tenant_id=tenant,
        )
    return t.id


async def test_invalid_init_data(api_client, settings):
    r = await api_client.get("/api/tickets", params={"initData": "hash=bad"})
    assert r.status_code == 401


async def test_no_role_forbidden(api_client, db, settings):
    async with db.session_factory() as s, s.begin():
        await users_svc.upsert_user(s, telegram_id=111)
    r = await api_client.get("/api/tickets", params=_auth(111, settings))
    assert r.status_code == 403


async def test_list_all_and_mine(api_client, db, settings):
    ids = await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)  # support
    t2 = await _make_ticket(db, settings, 111)  # admin
    # назначим t2 на dev 333, t1 — на admin 111 (admin видит и свои, и назначенные)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, t2)
        await tickets_svc.take_ticket(s, settings, ids[333], t, expected_version=t.version)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, t1)
        await tickets_svc.take_ticket(s, settings, ids[111], t, expected_version=t.version)
    # support: mine = созданные мной
    r = await api_client.get("/api/tickets", params=_auth(222, settings) | {"tab": "mine"})
    assert [i["id"] for i in r.json()["items"]] == [t1]
    # developer: mine = назначенные мне
    r = await api_client.get("/api/tickets", params=_auth(333, settings) | {"tab": "mine"})
    assert [i["id"] for i in r.json()["items"]] == [t2]
    # admin: mine = обе
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"tab": "mine"})
    assert sorted(i["id"] for i in r.json()["items"]) == sorted([t1, t2])
    # all — все
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"tab": "all"})
    assert r.json()["total"] == 2


async def test_filters_pagination_sort(api_client, db, settings):
    await _setup_roles(db, settings)
    ids = [await _make_ticket(db, settings, 222, priority=p) for p in ("critical", "high", "low")]
    r = await api_client.get(
        "/api/tickets",
        params=_auth(111, settings) | {"status": "new", "priority": "critical"},
    )
    assert [i["id"] for i in r.json()["items"]] == [ids[0]]
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"per_page": "2"})
    page1 = [i["id"] for i in r.json()["items"]]
    r = await api_client.get(
        "/api/tickets", params=_auth(111, settings) | {"page": "2", "per_page": "2"}
    )
    page2 = [i["id"] for i in r.json()["items"]]
    assert len(page1) == 2 and len(page2) == 1
    assert r.json()["total"] == 3
    # сортировка: новые сверху
    all_ids = page1 + page2
    assert all_ids == sorted(all_ids, reverse=True)
    # invalid filter
    r = await api_client.get("/api/tickets", params=_auth(111, settings) | {"status": "bogus"})
    assert r.status_code == 422


async def test_card(api_client, db, settings):
    await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)
    r = await api_client.get(f"/api/tickets/{t1}", params=_auth(111, settings))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == t1 and body["status"] == "new" and body["version"] == 1
    assert body["author_name"].startswith("U222")
    assert body["assignee_name"] is None
    r = await api_client.get("/api/tickets/99999", params=_auth(111, settings))
    assert r.status_code == 404


# tests/integration/test_api.py — часть 2: действия, users, роли


async def test_reopen_flow(api_client, db, settings, bot):
    ids = await _setup_roles(db, settings)
    tid = await _make_ticket(db, settings, 222)
    async with db.session_factory() as s, s.begin():
        ticket = await tickets_svc.load_ticket(s, tid)
        ticket = await tickets_svc.take_ticket(s, settings, ids[333], ticket, expected_version=1)
        await tickets_svc.apply_transition(
            s, settings, ids[333], ticket, expected_version=2, action="to_resolved"
        )
    # dev — 403
    r = await api_client.post(
        f"/api/tickets/{tid}/reopen", json={"expected_version": 3}, params=_auth(333, settings)
    )
    assert r.status_code == 403
    # admin, неверная версия — 409
    r = await api_client.post(
        f"/api/tickets/{tid}/reopen", json={"expected_version": 1}, params=_auth(111, settings)
    )
    assert r.status_code == 409
    # admin, верная версия — 200
    r = await api_client.post(
        f"/api/tickets/{tid}/reopen", json={"expected_version": 3}, params=_auth(111, settings)
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "new"
    assert body["version"] == 4
    assert body["assignee_name"] is None
    # пост ресинхронизирован (новый статус)
    assert any("новое" in m.get("text", "") for m in bot.sent)


async def test_reassign_flow(api_client, db, settings):
    ids = await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, t1)
        await tickets_svc.take_ticket(s, settings, ids[333], t, expected_version=1)
    # на support — 403
    r = await api_client.post(
        f"/api/tickets/{t1}/reassign",
        json={"user_id": ids[222], "expected_version": 2},
        params=_auth(111, settings),
    )
    assert r.status_code == 403
    # не admin — 403
    r = await api_client.post(
        f"/api/tickets/{t1}/reassign",
        json={"user_id": ids[444], "expected_version": 2},
        params=_auth(333, settings),
    )
    assert r.status_code == 403
    # на developer — 200
    r = await api_client.post(
        f"/api/tickets/{t1}/reassign",
        json={"user_id": ids[444], "expected_version": 2},
        params=_auth(111, settings),
    )
    assert r.status_code == 200
    assert r.json()["assignee_name"].startswith("U444")
    assert r.json()["version"] == 3
    # устаревшая версия — 409
    r = await api_client.post(
        f"/api/tickets/{t1}/reassign",
        json={"user_id": ids[444], "expected_version": 2},
        params=_auth(111, settings),
    )
    assert r.status_code == 409


async def test_restore_post(api_client, db, settings, bot):
    await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 222)
    # «теряем» пост: указываем несуществующий message_id
    async with db.session_factory() as s, s.begin():
        await s.execute(
            text("UPDATE tickets SET message_id = 55, post_synced_version = 1 WHERE id = :i"),
            {"i": t1},
        )
    r = await api_client.post(f"/api/tickets/{t1}/restore-post", params=_auth(111, settings))
    assert r.status_code == 200
    assert len(bot.sent) == 1
    assert bot.sent[0]["message_id"] == 1
    # не admin — 403
    r = await api_client.post(f"/api/tickets/{t1}/restore-post", params=_auth(222, settings))
    assert r.status_code == 403


async def test_list_users_admin_only(api_client, db, settings):
    await _setup_roles(db, settings)
    r = await api_client.get("/api/users", params=_auth(222, settings))
    assert r.status_code == 403
    r = await api_client.get("/api/users", params=_auth(111, settings))
    assert r.status_code == 200
    assert len(r.json()) == 4


async def test_role_change_api(api_client, db, settings):
    ids = await _setup_roles(db, settings)
    t1 = await _make_ticket(db, settings, 333)
    async with db.session_factory() as s, s.begin():
        t = await tickets_svc.load_ticket(s, t1)
        await tickets_svc.take_ticket(s, settings, ids[333], t, expected_version=1)
    # dev с активным обращением — 409
    r = await api_client.put(
        f"/api/users/{ids[333]}/role", json={"role": "support"}, params=_auth(111, settings)
    )
    assert r.status_code == 409
    assert r.json()["detail"]["tickets"] == [t1]
