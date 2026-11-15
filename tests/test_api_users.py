"""Mini App roles endpoints: admin gating, demotion guard, candidate picker."""

from httpx import AsyncClient

from app.config import settings
from app.db.models import Role, User
from app.domain.state_machine import Action
from app.services import tickets as tickets_svc
from tests.test_api_tickets import add_user, hdr, mk_ticket, take

ADMIN = 840
DEV = 841
SUPPORT = 842
OUTSIDER = 843


async def test_users_list_requires_admin(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, SUPPORT, Role.support)
    r = await api_client.get("/api/users", headers=hdr(SUPPORT))
    assert r.status_code == 403


async def test_users_list_and_search(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, ADMIN, Role.admin)
    await add_user(session_factory, DEV, Role.developer)
    await add_user(session_factory, OUTSIDER, None)

    body = (await api_client.get("/api/users", headers=hdr(ADMIN))).json()
    ids = {u["telegram_id"] for u in body["items"]}
    assert {ADMIN, DEV} <= ids

    hit = (await api_client.get("/api/users?q=841", headers=hdr(ADMIN))).json()
    assert [u["telegram_id"] for u in hit["items"]] == [DEV]


async def test_executor_picker_excludes_support(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, ADMIN, Role.admin)
    await add_user(session_factory, DEV, Role.developer)
    await add_user(session_factory, SUPPORT, Role.support)
    body = (await api_client.get("/api/users?executors_only=true", headers=hdr(ADMIN))).json()
    assert {u["telegram_id"] for u in body["items"]} == {ADMIN, DEV}


async def test_grant_and_revoke_role(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, ADMIN, Role.admin)
    async with session_factory() as s, s.begin():
        s.add(User(telegram_id=OUTSIDER, username="out", full_name="Outsider", role=None))

    r = await api_client.put(
        f"/api/users/{OUTSIDER}/role", json={"role": "developer"}, headers=hdr(ADMIN)
    )
    assert r.status_code == 200 and r.json()["role"] == "developer"

    r = await api_client.put(f"/api/users/{OUTSIDER}/role", json={"role": None}, headers=hdr(ADMIN))
    assert r.status_code == 200 and r.json()["role"] is None


async def test_unknown_role_and_missing_target(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, ADMIN, Role.admin)
    bad = await api_client.put(
        f"/api/users/{OUTSIDER}/role", json={"role": "wizard"}, headers=hdr(ADMIN)
    )
    assert bad.status_code == 422
    missing = await api_client.put(
        "/api/users/99999/role", json={"role": "developer"}, headers=hdr(ADMIN)
    )
    assert missing.status_code == 404


async def test_demotion_blocked_while_active_then_allowed(
    api_client: AsyncClient, session_factory
) -> None:
    await add_user(session_factory, ADMIN, Role.admin)
    await add_user(session_factory, DEV, Role.developer)
    tid = await mk_ticket(session_factory, author=ADMIN)
    await take(session_factory, tid, DEV)

    r = await api_client.put(f"/api/users/{DEV}/role", json={"role": "support"}, headers=hdr(ADMIN))
    assert r.status_code == 409
    assert r.json()["active_ticket_ids"] == [tid]

    # Admin reassigns to clear the assignment, demotion via API then passes.
    reassigned = await api_client.post(
        f"/api/tickets/{tid}/reassign",
        json={"expected_version": 2, "target_telegram_id": ADMIN},
        headers=hdr(ADMIN),
    )
    assert reassigned.status_code == 200

    r2 = await api_client.put(
        f"/api/users/{DEV}/role", json={"role": "support"}, headers=hdr(ADMIN)
    )
    assert r2.status_code == 200 and r2.json()["role"] == "support"


async def test_abandon_unblocks_demotion(api_client: AsyncClient, session_factory) -> None:
    await add_user(session_factory, ADMIN, Role.admin)
    await add_user(session_factory, DEV, Role.developer)
    tid = await mk_ticket(session_factory, author=ADMIN)
    await take(session_factory, tid, DEV)
    async with session_factory() as s, s.begin():
        await tickets_svc.apply_action(
            s, settings, ticket_id=tid, actor_id=ADMIN, action=Action.abandon, expected_version=2
        )
    r = await api_client.put(f"/api/users/{DEV}/role", json={"role": None}, headers=hdr(ADMIN))
    assert r.status_code == 200
