"""Mini App ticket endpoints: auth gating, scoping, filters, admin actions."""

from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.db.models import Role, Ticket, User
from app.domain.state_machine import Action
from app.main import create_app
from app.services import tickets as tickets_svc
from tests.conftest import tma_header, user_pairs

SUPPORT = 830
SUPPORT2 = 835
DEV = 831
DEV2 = 832
ADMIN = 833


async def add_user(sf, tid: int, role: Role | None) -> None:
    async with sf() as s, s.begin():
        s.add(User(telegram_id=tid, username=f"u{tid}", full_name=f"User {tid}", role=role))


async def mk_ticket(
    sf,
    *,
    author: int = SUPPORT,
    priority: str = "high",
    desc: str = "Течь под мойкой",
    rid: str = "RQ-1",
) -> int:
    async with sf() as s, s.begin():
        ticket = await tickets_svc.create(
            s, settings, author_id=author, description=desc, priority=priority,
            request_id=rid, tenant_id=None,
        )
        return ticket.id


async def act(sf, tid: int, actor: int, **kw) -> None:
    async with sf() as s, s.begin():
        await tickets_svc.apply_action(s, settings, ticket_id=tid, actor_id=actor, **kw)


async def take(sf, tid: int, actor: int, version: int = 1) -> None:
    async with sf() as s, s.begin():
        await tickets_svc.take(s, settings, ticket_id=tid, actor_id=actor, expected_version=version)


def hdr(tid: int) -> dict[str, str]:
    return tma_header(user_pairs(tid, f"u{tid}", f"User {tid}"))


async def test_missing_or_bad_auth(api_client: AsyncClient) -> None:
    r = await api_client.get("/api/tickets")
    assert r.status_code == 401
    r = await api_client.get("/api/tickets", headers={"Authorization": "tma auth_date=1&hash=zz"})
    assert r.status_code == 401


async def test_roleless_user_forbidden(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, SUPPORT, None)
    r = await api_client.get("/api/tickets", headers=hdr(SUPPORT))
    assert r.status_code == 403


async def test_me_returns_fresh_role(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, ADMIN, Role.admin)
    r = await api_client.get("/api/me", headers=hdr(ADMIN))
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    assert body["telegram_id"] == ADMIN


async def test_list_scoping_and_filters(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    await add_user(session_factory, SUPPORT2, Role.support)
    await add_user(session_factory, DEV, Role.developer)
    t1 = await mk_ticket(session_factory, priority="high", rid="RQ-1")
    t2 = await mk_ticket(session_factory, priority="low", rid="RQ-2")
    t3 = await mk_ticket(session_factory, author=SUPPORT2, priority="critical", rid="RQ-3")
    await take(session_factory, t2, DEV)

    mine = (await api_client.get("/api/tickets?scope=mine", headers=hdr(SUPPORT))).json()
    assert {t["id"] for t in mine["items"]} == {t1, t2}
    dev_mine = (await api_client.get("/api/tickets?scope=mine", headers=hdr(DEV))).json()
    assert {t["id"] for t in dev_mine["items"]} == {t2}
    all_r = await api_client.get("/api/tickets?scope=all", headers=hdr(SUPPORT))
    assert all_r.json()["total"] == 3

    only_new = (await api_client.get("/api/tickets?scope=all&status=new", headers=hdr(DEV))).json()
    assert {t["id"] for t in only_new["items"]} == {t1, t3}
    prio = (await api_client.get("/api/tickets?scope=all&priority=low", headers=hdr(DEV))).json()
    assert [t["id"] for t in prio["items"]] == [t2]
    bad = await api_client.get("/api/tickets?status=nope", headers=hdr(DEV))
    assert bad.status_code == 422

    page = (
        await api_client.get("/api/tickets?scope=all&limit=1&offset=1", headers=hdr(DEV))
    ).json()
    assert len(page["items"]) == 1 and page["total"] == 3
    # Newest first: created order t1 < t2 < t3.
    first = (await api_client.get("/api/tickets?scope=all&limit=1", headers=hdr(DEV))).json()
    assert first["items"][0]["id"] == max(t1, t2, t3)


async def test_card_fields_and_missing(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    tid = await mk_ticket(session_factory)
    r = await api_client.get(f"/api/tickets/{tid}", headers=hdr(SUPPORT))
    card = r.json()
    assert card["description"] == "Течь под мойкой"
    assert card["request_id"] == "RQ-1"
    assert card["status"] == "new" and card["priority"] == "high" and card["version"] == 1
    assert card["author"]["telegram_id"] == SUPPORT
    assert card["assignee"] is None
    assert (await api_client.get("/api/tickets/999999", headers=hdr(SUPPORT))).status_code == 404


async def test_admin_reopen_bumps_version_and_syncs(
    api_client: AsyncClient, session_factory, api_app
) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    await add_user(session_factory, DEV, Role.developer)
    await add_user(session_factory, ADMIN, Role.admin)
    tid = await mk_ticket(session_factory)
    await take(session_factory, tid, DEV)
    await act(session_factory, tid, DEV, action=Action.resolve, expected_version=2)

    r = await api_client.post(
        f"/api/tickets/{tid}/reopen", json={"expected_version": 3}, headers=hdr(ADMIN)
    )
    assert r.status_code == 200
    card = r.json()
    assert card["status"] == "new"
    assert card["assignee"] is None
    assert card["version"] == 4
    assert card["post_synced"] is True
    fake = api_app.state.gateway
    assert fake.calls and fake.calls[-1].kind == "send"
    assert card["message_id"] == fake.calls[-1].message_id


async def test_reopen_stale_version_409(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    await add_user(session_factory, DEV, Role.developer)
    await add_user(session_factory, ADMIN, Role.admin)
    tid = await mk_ticket(session_factory)
    await take(session_factory, tid, DEV)
    await act(session_factory, tid, DEV, action=Action.resolve, expected_version=2)
    # Real version is 3 now; a stale request must answer 409, not apply blindly.
    r = await api_client.post(
        f"/api/tickets/{tid}/reopen", json={"expected_version": 2}, headers=hdr(ADMIN)
    )
    assert r.status_code == 409
    assert r.json()["error"] == "Обращение изменилось"


async def test_reopen_by_nonadmin_403(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    await add_user(session_factory, DEV, Role.developer)
    tid = await mk_ticket(session_factory)
    await take(session_factory, tid, DEV)
    await act(session_factory, tid, DEV, action=Action.resolve, expected_version=2)
    r = await api_client.post(
        f"/api/tickets/{tid}/reopen", json={"expected_version": 3}, headers=hdr(SUPPORT)
    )
    assert r.status_code == 403


async def test_reassign_keeps_status(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    await add_user(session_factory, DEV, Role.developer)
    await add_user(session_factory, DEV2, Role.developer)
    await add_user(session_factory, ADMIN, Role.admin)
    tid = await mk_ticket(session_factory)
    await take(session_factory, tid, DEV)

    r = await api_client.post(
        f"/api/tickets/{tid}/reassign",
        json={"expected_version": 2, "target_telegram_id": DEV2},
        headers=hdr(ADMIN),
    )
    assert r.status_code == 200
    card = r.json()
    assert card["status"] == "in_progress"
    assert card["assignee"]["telegram_id"] == DEV2
    assert card["version"] == 3

    bad = await api_client.post(
        f"/api/tickets/{tid}/reassign",
        json={"expected_version": 3, "target_telegram_id": SUPPORT},
        headers=hdr(ADMIN),
    )
    assert bad.status_code == 422  # support cannot take


async def test_restore_post_no_version_bump(
    api_client: AsyncClient, session_factory, api_app
) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    await add_user(session_factory, ADMIN, Role.admin)
    tid = await mk_ticket(session_factory)
    fake = api_app.state.gateway

    first = (await api_client.post(f"/api/tickets/{tid}/restore_post", headers=hdr(ADMIN))).json()
    assert first["version"] == 1
    assert first["post_synced"] is True
    msg_a = first["message_id"]

    second = (await api_client.post(f"/api/tickets/{tid}/restore_post", headers=hdr(ADMIN))).json()
    assert second["version"] == 1
    assert second["message_id"] != msg_a  # republished a fresh post
    assert second["message_id"] in fake.live


async def test_actions_without_gateway_still_succeed(
    api_client: AsyncClient, session_factory
) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    await add_user(session_factory, DEV, Role.developer)
    await add_user(session_factory, ADMIN, Role.admin)
    tid = await mk_ticket(session_factory)
    await take(session_factory, tid, DEV)
    await act(session_factory, tid, DEV, action=Action.resolve, expected_version=2)

    app = create_app(bot_enabled=False)
    app.state.session_factory = session_factory
    app.state.gateway = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            f"/api/tickets/{tid}/reopen", json={"expected_version": 3}, headers=hdr(ADMIN)
        )
        assert r.status_code == 200
        assert r.json()["post_synced"] is False
    status = await _db_status(session_factory, tid)
    assert status == "new"


async def _db_status(sf, tid: int) -> str:
    async with sf() as s:
        ticket = await s.get(Ticket, tid)
        assert ticket is not None
        return str(ticket.status)
