import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from support_bot.api.auth import InitDataError, sign_init_data, validate_init_data
from support_bot.config import Settings
from support_bot.container import Container, build_container
from support_bot.domain.enums import Priority, Role, TicketStatus
from support_bot.domain.state_machine import Action
from support_bot.main import build_app
from support_bot.services.views import UserInfo
from tests.conftest import BOT_TOKEN, FakePublisher, MakeTicket, MakeUser


def init_data(
    user: UserInfo | dict[str, Any], auth_date: int | None = None, token: str = BOT_TOKEN
) -> str:
    tg_user = (
        user
        if isinstance(user, dict)
        else {"id": user.telegram_id, "first_name": user.first_name, "username": user.username}
    )
    fields = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAH",
        "user": json.dumps(tg_user, ensure_ascii=False, separators=(",", ":")),
        "signature": "sig",
    }
    return sign_init_data(fields, token)


def auth(user: UserInfo) -> dict[str, str]:
    return {"Authorization": f"tma {init_data(user)}"}


# --- initData ------------------------------------------------------------------------------


def test_init_data_valid() -> None:
    data = init_data({"id": 42, "first_name": "Иван", "username": "ivan"})
    user = validate_init_data(data, BOT_TOKEN, 3600)
    assert (user.id, user.first_name, user.username) == (42, "Иван", "ivan")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.replace("Ivan", "Petr"),  # подмена данных
        lambda d: d + "&extra=1",  # добавленное поле
        lambda d: d.split("&hash=")[0] if "&hash=" in d else d.replace("hash=", "h="),
        lambda d: "garbage",
    ],
)
def test_init_data_tampered(mutate: Any) -> None:
    data = init_data({"id": 42, "first_name": "Ivan"})
    with pytest.raises(InitDataError):
        validate_init_data(mutate(data), BOT_TOKEN, 3600)


def test_init_data_wrong_token() -> None:
    data = init_data({"id": 42}, token="999:other")
    with pytest.raises(InitDataError):
        validate_init_data(data, BOT_TOKEN, 3600)


def test_init_data_expired_and_future() -> None:
    old = init_data({"id": 42}, auth_date=int(time.time()) - 7200)
    with pytest.raises(InitDataError):
        validate_init_data(old, BOT_TOKEN, 3600)
    future = init_data({"id": 42}, auth_date=int(time.time()) + 3600)
    with pytest.raises(InitDataError):
        validate_init_data(future, BOT_TOKEN, 3600)


# --- API -----------------------------------------------------------------------------------


@pytest.fixture
def api_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"webhook_secret": SecretStr("s3cret")})


@pytest.fixture
def container(api_settings: Settings, engine: AsyncEngine, publisher: FakePublisher) -> Container:
    return build_container(api_settings, engine, publisher)


class FakePipeline:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def handle(self, update: Any) -> str:
        self.updates.append(update)
        return "processed"


class FakeBotApp:
    bot = None


@pytest.fixture
def pipeline() -> FakePipeline:
    return FakePipeline()


@pytest.fixture
async def client(
    api_settings: Settings, container: Container, pipeline: FakePipeline
) -> AsyncIterator[httpx.AsyncClient]:
    app = build_app(api_settings, container=container, pipeline=pipeline, bot_app=FakeBotApp())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health/live")).status_code == 200
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["x-request-id"]


async def test_auth_required(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    assert (await client.get("/api/me")).status_code == 401
    bad = {"Authorization": "tma auth_date=1&hash=00"}
    assert (await client.get("/api/me", headers=bad)).status_code == 401
    nobody = await make_user(None)
    r = await client.get("/api/me", headers=auth(nobody))
    assert r.status_code == 403 and r.json()["error"] == "no_access"


async def test_unknown_user_registered_without_access(client: httpx.AsyncClient) -> None:
    headers = {"Authorization": f"tma {init_data({'id': 555, 'first_name': 'New'})}"}
    assert (await client.get("/api/me", headers=headers)).status_code == 403


async def test_me(client: httpx.AsyncClient, make_user: MakeUser) -> None:
    admin = await make_user(Role.ADMIN, username="boss")
    body = (await client.get("/api/me", headers=auth(admin))).json()
    assert body["role"] == "admin" and body["is_admin"] and body["can_create"]
    assert body["can_be_assignee"] and body["username"] == "boss"


async def test_lists_scopes_filters_pagination(
    client: httpx.AsyncClient, container: Container, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, other, dev = (
        await make_user(Role.SUPPORT),
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
    )
    mine = [await make_ticket(support, Priority.HIGH) for _ in range(3)]
    theirs = await make_ticket(other, Priority.LOW)
    await container.tickets.apply_action(dev.id, mine[0], Action.TAKE, 1)

    r = await client.get("/api/tickets", params={"scope": "created"}, headers=auth(support))
    body = r.json()
    assert [t["id"] for t in body["items"]] == sorted(mine, reverse=True)  # новые сверху

    r = await client.get("/api/tickets", params={"scope": "all"}, headers=auth(support))
    assert r.json()["total"] == 4

    r = await client.get(
        "/api/tickets", params={"scope": "all", "priority": "low"}, headers=auth(dev)
    )
    assert [t["id"] for t in r.json()["items"]] == [theirs]

    r = await client.get("/api/tickets", params={"scope": "assigned"}, headers=auth(dev))
    assert [t["id"] for t in r.json()["items"]] == [mine[0]]

    r = await client.get(
        "/api/tickets",
        params=[("scope", "all"), ("status", "new"), ("status", "in_progress"), ("page_size", "2")],
        headers=auth(dev),
    )
    body = r.json()
    assert body["total"] == 4 and len(body["items"]) == 2
    r = await client.get(
        "/api/tickets", params={"scope": "all", "page": 2, "page_size": 2}, headers=auth(dev)
    )
    assert len(r.json()["items"]) == 2

    # support не видит «назначенные мне», developer — «созданные мной»
    assert (
        await client.get("/api/tickets", params={"scope": "assigned"}, headers=auth(support))
    ).status_code == 403
    assert (
        await client.get("/api/tickets", params={"scope": "created"}, headers=auth(dev))
    ).status_code == 403


async def test_ticket_card(
    client: httpx.AsyncClient, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, admin = await make_user(Role.SUPPORT), await make_user(Role.ADMIN)
    tid = await make_ticket(support, description="Полное описание " * 100, tenant_id="T-1")
    body = (await client.get(f"/api/tickets/{tid}", headers=auth(support))).json()
    assert body["description"].startswith("Полное описание") and body["tenant_id"] == "T-1"
    assert body["permissions"] == {
        "can_reopen": False,
        "can_reassign": False,
        "can_restore_post": False,
    }
    body = (await client.get(f"/api/tickets/{tid}", headers=auth(admin))).json()
    assert body["permissions"]["can_restore_post"] is True
    assert (await client.get("/api/tickets/999999", headers=auth(admin))).status_code == 404


async def test_admin_reopen_and_reassign_sync_post(
    client: httpx.AsyncClient,
    container: Container,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support, d1, d2, admin = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.DEVELOPER),
        await make_user(Role.ADMIN),
    )
    tid = await make_ticket(support)
    await container.post_sync.sync(tid)
    await container.tickets.apply_action(d1.id, tid, Action.TAKE, 1)

    # не-admin не может переназначить
    r = await client.post(
        f"/api/tickets/{tid}/reassign", json={"version": 2, "assignee_id": d2.id}, headers=auth(d1)
    )
    assert r.status_code == 403
    r = await client.post(
        f"/api/tickets/{tid}/reassign",
        json={"version": 2, "assignee_id": d2.id},
        headers=auth(admin),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assignee"]["id"] == d2.id and body["version"] == 3
    message_id = next(iter(publisher.posts))
    assert d2.first_name and d2.first_name in publisher.posts[message_id][0]

    # устаревшая version → 409
    r = await client.post(
        f"/api/tickets/{tid}/reassign",
        json={"version": 2, "assignee_id": d1.id},
        headers=auth(admin),
    )
    assert r.status_code == 409 and r.json()["error"] == "stale_version"

    await container.tickets.apply_action(d2.id, tid, Action.RESOLVE, 3)
    r = await client.post(f"/api/tickets/{tid}/reopen", json={"version": 4}, headers=auth(d2))
    assert r.status_code == 403
    r = await client.post(f"/api/tickets/{tid}/reopen", json={"version": 4}, headers=auth(admin))
    assert r.status_code == 200
    assert r.json()["status"] == TicketStatus.NEW.value and r.json()["assignee"] is None
    assert "Новое" in publisher.posts[message_id][0]


async def test_restore_post(
    client: httpx.AsyncClient,
    container: Container,
    publisher: FakePublisher,
    make_user: MakeUser,
    make_ticket: MakeTicket,
) -> None:
    support, admin = await make_user(Role.SUPPORT), await make_user(Role.ADMIN)
    tid = await make_ticket(support)
    await container.post_sync.sync(tid)
    r = await client.post(f"/api/tickets/{tid}/restore-post", headers=auth(support))
    assert r.status_code == 403
    r = await client.post(f"/api/tickets/{tid}/restore-post", headers=auth(admin))
    assert r.status_code == 200 and r.json()["version"] == 1
    assert len(publisher.sent) == 2


async def test_roles_api(
    client: httpx.AsyncClient, container: Container, make_user: MakeUser, make_ticket: MakeTicket
) -> None:
    support, dev, admin, nobody = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.ADMIN),
        await make_user(None),
    )
    assert (await client.get("/api/users", headers=auth(dev))).status_code == 403
    users = (await client.get("/api/users", headers=auth(admin))).json()
    assert {u["id"] for u in users} == {support.id, dev.id, admin.id, nobody.id}
    assignees = (await client.get("/api/users/assignees", headers=auth(admin))).json()
    assert {u["id"] for u in assignees} == {dev.id, admin.id}

    r = await client.put(
        f"/api/users/{nobody.id}/role", json={"role": "support"}, headers=auth(admin)
    )
    assert r.status_code == 200 and r.json()["role"] == "support"

    tid = await make_ticket(support)
    await container.tickets.apply_action(dev.id, tid, Action.TAKE, 1)
    r = await client.put(f"/api/users/{dev.id}/role", json={"role": None}, headers=auth(admin))
    assert r.status_code == 409
    assert r.json()["error"] == "role_change_blocked" and r.json()["tickets"] == [tid]

    r = await client.put(
        f"/api/users/{admin.id}/role", json={"role": "developer"}, headers=auth(admin)
    )
    assert r.status_code == 422
    r = await client.put(f"/api/users/{dev.id}/role", json={"role": "bogus"}, headers=auth(admin))
    assert r.status_code == 422


async def test_webhook_secret(client: httpx.AsyncClient, pipeline: FakePipeline) -> None:
    payload = {"update_id": 1}
    r = await client.post("/telegram/webhook", json=payload)
    assert r.status_code == 403
    r = await client.post(
        "/telegram/webhook", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
    )
    assert r.status_code == 403
    r = await client.post(
        "/telegram/webhook", json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
    )
    assert r.status_code == 200
    assert [u.update_id for u in pipeline.updates] == [1]


async def test_static_webapp(client: httpx.AsyncClient) -> None:
    r = await client.get("/app/")
    assert r.status_code == 200 and "telegram-web-app.js" in r.text
    assert (await client.get("/app/app.js")).status_code == 200
