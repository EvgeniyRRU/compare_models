from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.bot.app import build_application
from support_bot.config import Settings
from support_bot.db.models import Ticket, User
from support_bot.domain.state_machine import Action, Status
from support_bot.services import tickets as ticket_svc
from support_bot.web.app import create_app
from tests.fake_telegram import FakeTelegram
from tests.helpers import NOW, add_user, signed_init_data


@pytest.fixture
async def client(
    settings: Settings, engine: Any, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[tuple[AsyncClient, FakeTelegram]]:
    fake = FakeTelegram()
    application = build_application(settings, engine, session_factory, request=fake)
    await application.initialize()
    app = create_app()
    app.state.settings = settings
    app.state.runtime = application.bot_data["runtime"]
    app.state.application = application
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, fake
    await application.shutdown()


def _auth(tg_id: int) -> dict[str, str]:
    return {"Authorization": f"tma {signed_init_data(tg_id)}"}


async def _ticket(
    session: AsyncSession, settings: Settings, author: int, priority: str = "high"
) -> int:
    ticket = await ticket_svc.create_ticket(
        session,
        settings=settings,
        actor_id=author,
        description="Описание",
        priority=priority,
        request_id="REQ",
        tenant_id=None,
        now=NOW,
    )
    await session.commit()
    return ticket.id


async def test_requests_without_valid_init_data_are_unauthorized(
    client: tuple[AsyncClient, FakeTelegram],
) -> None:
    http, _ = client

    no_header = await http.get("/api/me")
    forged = await http.get("/api/me", headers={"Authorization": "tma auth_date=1&hash=bad"})

    assert no_header.status_code == 401
    assert forged.status_code == 401


async def test_user_without_role_gets_no_ticket_access(
    client: tuple[AsyncClient, FakeTelegram], session: AsyncSession
) -> None:
    http, _ = client
    await add_user(session, 8001, None)

    response = await http.get("/api/tickets", headers=_auth(8001))

    assert response.status_code == 403


async def test_support_sees_own_created_tickets_and_developer_sees_assigned(
    client: tuple[AsyncClient, FakeTelegram], session: AsyncSession, settings: Settings
) -> None:
    http, _ = client
    support = await add_user(session, 8010, "support")
    other = await add_user(session, 8011, "support")
    dev = await add_user(session, 8012, "developer")
    mine = await _ticket(session, settings, support)
    await _ticket(session, settings, other)
    await ticket_svc.transition(
        session,
        settings=settings,
        actor_id=dev,
        ticket_id=mine,
        action=Action.TAKE,
        expected_version=1,
        expected_status=Status.NEW,
        now=NOW,
    )
    await session.commit()

    support_view = await http.get("/api/tickets?scope=created", headers=_auth(8010))
    dev_view = await http.get("/api/tickets?scope=assigned", headers=_auth(8012))

    assert [t["id"] for t in support_view.json()["items"]] == [mine]
    assert [t["id"] for t in dev_view.json()["items"]] == [mine]


async def test_tickets_are_newest_first_and_paginated_with_filters(
    client: tuple[AsyncClient, FakeTelegram], session: AsyncSession, settings: Settings
) -> None:
    http, _ = client
    support = await add_user(session, 8020, "support")
    ids = [await _ticket(session, settings, support, "low") for _ in range(3)]
    urgent = await _ticket(session, settings, support, "critical")

    page1 = await http.get("/api/tickets?size=2&page=1", headers=_auth(8020))
    critical = await http.get("/api/tickets?priority=critical", headers=_auth(8020))

    body = page1.json()
    assert body["total"] == 4
    assert [t["id"] for t in body["items"]] == [urgent, ids[2]]
    assert [t["id"] for t in critical.json()["items"]] == [urgent]


async def test_card_shows_admin_actions_only_to_admin(
    client: tuple[AsyncClient, FakeTelegram], session: AsyncSession, settings: Settings
) -> None:
    http, _ = client
    support = await add_user(session, 8030, "support")
    await add_user(session, 8031, "admin")
    ticket_id = await _ticket(session, settings, support)

    as_support = await http.get(f"/api/tickets/{ticket_id}", headers=_auth(8030))
    as_admin = await http.get(f"/api/tickets/{ticket_id}", headers=_auth(8031))

    assert as_support.json()["can_restore_post"] is False
    assert as_admin.json()["can_restore_post"] is True


async def test_only_admin_reopens_and_version_is_checked(
    client: tuple[AsyncClient, FakeTelegram], session: AsyncSession, settings: Settings
) -> None:
    http, _ = client
    support = await add_user(session, 8040, "support")
    dev = await add_user(session, 8041, "developer")
    await add_user(session, 8042, "admin")
    ticket_id = await _ticket(session, settings, support)
    await ticket_svc.transition(
        session,
        settings=settings,
        actor_id=dev,
        ticket_id=ticket_id,
        action=Action.TAKE,
        expected_version=1,
        expected_status=Status.NEW,
        now=NOW,
    )
    await ticket_svc.transition(
        session,
        settings=settings,
        actor_id=dev,
        ticket_id=ticket_id,
        action=Action.RESOLVE,
        expected_version=2,
        expected_status=Status.IN_PROGRESS,
        now=NOW,
    )
    await session.commit()

    denied = await http.post(
        f"/api/tickets/{ticket_id}/reopen", headers=_auth(8041), json={"expected_version": 3}
    )
    stale = await http.post(
        f"/api/tickets/{ticket_id}/reopen", headers=_auth(8042), json={"expected_version": 2}
    )
    ok = await http.post(
        f"/api/tickets/{ticket_id}/reopen", headers=_auth(8042), json={"expected_version": 3}
    )

    assert denied.status_code == 403
    assert stale.status_code == 409
    assert ok.status_code == 200
    assert ok.json()["status"] == "new" and ok.json()["version"] == 4


async def test_reassign_target_must_be_developer_or_admin(
    client: tuple[AsyncClient, FakeTelegram], session: AsyncSession, settings: Settings
) -> None:
    http, _ = client
    support = await add_user(session, 8050, "support")
    dev = await add_user(session, 8051, "developer")
    admin = await add_user(session, 8052, "admin")
    plain = await add_user(session, 8053, "support")
    ticket_id = await _ticket(session, settings, support)
    await ticket_svc.transition(
        session,
        settings=settings,
        actor_id=dev,
        ticket_id=ticket_id,
        action=Action.TAKE,
        expected_version=1,
        expected_status=Status.NEW,
        now=NOW,
    )
    await session.commit()

    wrong = await http.post(
        f"/api/tickets/{ticket_id}/reassign",
        headers=_auth(8052),
        json={"expected_version": 2, "assignee_id": plain},
    )
    right = await http.post(
        f"/api/tickets/{ticket_id}/reassign",
        headers=_auth(8052),
        json={"expected_version": 2, "assignee_id": admin},
    )

    assert wrong.status_code == 422
    assert right.status_code == 200
    assert right.json()["assignee_id"] == admin and right.json()["status"] == "in_progress"


async def test_role_change_is_refused_with_list_of_active_tickets(
    client: tuple[AsyncClient, FakeTelegram], session: AsyncSession, settings: Settings
) -> None:
    http, _ = client
    support = await add_user(session, 8060, "support")
    await add_user(session, 8061, "developer")
    await add_user(session, 8062, "admin")
    ticket_id = await _ticket(session, settings, support)
    dev_db_id = await session.scalar(select(User.id).where(User.telegram_id == 8061))
    assert dev_db_id is not None
    await ticket_svc.transition(
        session,
        settings=settings,
        actor_id=dev_db_id,
        ticket_id=ticket_id,
        action=Action.TAKE,
        expected_version=1,
        expected_status=Status.NEW,
        now=NOW,
    )
    await session.commit()

    response = await http.put("/api/users/8061/role", headers=_auth(8062), json={"role": "support"})

    assert response.status_code == 409
    assert response.json()["ticket_ids"] == [ticket_id]


async def test_admin_lists_users_and_changes_role(
    client: tuple[AsyncClient, FakeTelegram], session: AsyncSession
) -> None:
    http, _ = client
    await add_user(session, 8070, "admin")
    await add_user(session, 8071, None)

    listing = await http.get("/api/users", headers=_auth(8070))
    changed = await http.put(
        "/api/users/8071/role", headers=_auth(8070), json={"role": "developer"}
    )

    assert {u["telegram_id"] for u in listing.json()} == {8070, 8071}
    assert changed.json()["role"] == "developer"


async def test_restore_post_publishes_new_message_without_bumping_version(
    client: tuple[AsyncClient, FakeTelegram], session: AsyncSession, settings: Settings
) -> None:
    http, fake = client
    support = await add_user(session, 8080, "support")
    await add_user(session, 8081, "admin")
    ticket_id = await _ticket(session, settings, support)
    await http.post(
        f"/api/tickets/{ticket_id}/reopen", headers=_auth(8081), json={"expected_version": 1}
    )
    sends_before = len(fake.sent("sendMessage"))

    response = await http.post(f"/api/tickets/{ticket_id}/restore-post", headers=_auth(8081))

    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    assert response.status_code == 204
    assert len(fake.sent("sendMessage")) == sends_before + 1
    assert ticket is not None and ticket.version == 1 and ticket.message_id is not None


async def test_health_endpoints(client: tuple[AsyncClient, FakeTelegram]) -> None:
    http, _ = client

    live = await http.get("/health/live")
    ready = await http.get("/health/ready")

    assert live.status_code == 200
    assert ready.status_code == 200


async def test_mini_app_is_served(client: tuple[AsyncClient, FakeTelegram]) -> None:
    http, _ = client

    response = await http.get("/app/")

    assert response.status_code == 200
    assert "Обращения" in response.text
