from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from httpx import ASGITransport, AsyncClient

from support_bot.db.enums import TicketPriority, UserRole
from support_bot.db.models.ticket import Ticket
from support_bot.domain.state_machine import TicketAction
from support_bot.services import tickets as tickets_service


def sign_init_data(
    bot_token: str,
    telegram_id: int,
    *,
    auth_date: int | None = None,
    username: str = "tester",
) -> str:
    user = {"id": telegram_id, "first_name": "Test", "username": username}
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAH-test",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


@pytest.fixture
async def client(api_app):
    transport = ASGITransport(app=api_app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


def auth_headers(settings, telegram_id: int, **kwargs) -> dict[str, str]:
    token = sign_init_data(settings.bot_token_value, telegram_id, **kwargs)
    return {"Authorization": f"tma {token}"}


async def test_me_rejects_missing_tampered_and_expired_init_data(
    client, settings, make_user
) -> None:
    await make_user(telegram_id=500, role=UserRole.ADMIN)

    response = await client.get("/api/me")
    assert response.status_code == 401

    valid = sign_init_data(settings.bot_token_value, 500)
    tampered = valid[:-1] + ("0" if valid[-1] != "0" else "1")
    response = await client.get("/api/me", headers={"Authorization": f"tma {tampered}"})
    assert response.status_code == 401

    expired = sign_init_data(
        settings.bot_token_value,
        500,
        auth_date=int(time.time()) - settings.init_data_max_age_seconds - 5,
    )
    response = await client.get("/api/me", headers={"Authorization": f"tma {expired}"})
    assert response.status_code == 401


async def test_roleless_user_gets_403(client, settings) -> None:
    response = await client.get("/api/me", headers=auth_headers(settings, 501, username="norole"))
    assert response.status_code == 403


async def test_me_returns_role(client, settings, make_user) -> None:
    await make_user(telegram_id=502, role=UserRole.DEVELOPER, first_name="Dev")
    response = await client.get("/api/me", headers=auth_headers(settings, 502))
    assert response.status_code == 200
    payload = response.json()
    assert payload["user"]["role"] == "developer"
    assert payload["can_take"] is True
    assert payload["is_admin"] is False


async def test_ticket_scopes_filters_and_pagination(
    client, settings, session_factory, make_user, make_ticket
) -> None:
    support = await make_user(telegram_id=510, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=511, role=UserRole.DEVELOPER)
    first = await make_ticket(author_id=support.id, priority=TicketPriority.MEDIUM, request_id="A")
    second = await make_ticket(
        author_id=support.id, priority=TicketPriority.CRITICAL, request_id="B"
    )
    async with session_factory() as session:
        await tickets_service.take_ticket(
            session, ticket_id=first.id, expected_version=1, actor_id=developer.id
        )

    headers = auth_headers(settings, 510)
    response = await client.get("/api/tickets?scope=authored", headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] == 2

    response = await client.get("/api/tickets?scope=authored&priority=critical", headers=headers)
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["id"] == second.id

    response = await client.get("/api/tickets?scope=authored&status=in_progress", headers=headers)
    assert response.json()["total"] == 1

    response = await client.get("/api/tickets?scope=authored&page=2&page_size=1", headers=headers)
    assert response.json()["total"] == 2
    assert len(response.json()["items"]) == 1

    developer_headers = auth_headers(settings, 511)
    response = await client.get("/api/tickets?scope=assigned", headers=developer_headers)
    assert response.json()["total"] == 1

    response = await client.get(f"/api/tickets/{first.id}", headers=developer_headers)
    assert response.status_code == 200
    assert response.json()["assignee"]["id"] == developer.id


async def test_admin_reopen_and_version_conflict(
    client, settings, session_factory, make_user, make_ticket
) -> None:
    support = await make_user(telegram_id=520, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=521, role=UserRole.DEVELOPER)
    await make_user(telegram_id=522, role=UserRole.ADMIN)
    ticket = await make_ticket(author_id=support.id)
    async with session_factory() as session:
        taken = await tickets_service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )
    async with session_factory() as session:
        resolved = await tickets_service.change_status(
            session,
            ticket_id=ticket.id,
            expected_version=taken.version,
            action=TicketAction.RESOLVE,
            actor_id=developer.id,
            policy=_policy(),
        )

    developer_headers = auth_headers(settings, 521)
    response = await client.post(
        f"/api/tickets/{ticket.id}/reopen",
        json={"version": resolved.version},
        headers=developer_headers,
    )
    assert response.status_code == 403

    admin_headers = auth_headers(settings, 522)
    response = await client.post(
        f"/api/tickets/{ticket.id}/reopen",
        json={"version": resolved.version},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "new"
    assert response.json()["version"] == resolved.version + 1

    response = await client.post(
        f"/api/tickets/{ticket.id}/reopen",
        json={"version": resolved.version},
        headers=admin_headers,
    )
    assert response.status_code == 409


async def test_reassign_validates_target_and_version(
    client, settings, session_factory, make_user, make_ticket
) -> None:
    support = await make_user(telegram_id=530, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=531, role=UserRole.DEVELOPER)
    other = await make_user(telegram_id=532, role=UserRole.DEVELOPER)
    await make_user(telegram_id=533, role=UserRole.ADMIN)
    ticket = await make_ticket(author_id=support.id)
    async with session_factory() as session:
        taken = await tickets_service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )

    admin_headers = auth_headers(settings, 533)
    response = await client.post(
        f"/api/tickets/{ticket.id}/reassign",
        json={"version": taken.version, "assignee_id": support.id},
        headers=admin_headers,
    )
    assert response.status_code == 422

    response = await client.post(
        f"/api/tickets/{ticket.id}/reassign",
        json={"version": taken.version, "assignee_id": other.id},
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert response.json()["assignee"]["id"] == other.id

    response = await client.post(
        f"/api/tickets/{ticket.id}/reassign",
        json={"version": taken.version, "assignee_id": developer.id},
        headers=admin_headers,
    )
    assert response.status_code == 409


async def test_restore_post_publishes_new_message(
    client, settings, session_factory, publisher, make_user, make_ticket
) -> None:
    support = await make_user(telegram_id=540, role=UserRole.SUPPORT)
    await make_user(telegram_id=541, role=UserRole.ADMIN)
    ticket = await make_ticket(author_id=support.id)
    # First sync through the real service creates the initial post.
    async with session_factory() as session:
        current = await session.get(Ticket, ticket.id)
    assert current is not None

    admin_headers = auth_headers(settings, 541)
    response = await client.post(f"/api/tickets/{ticket.id}/restore-post", headers=admin_headers)
    assert response.status_code == 200
    assert len(publisher.sent) == 1

    response = await client.post(f"/api/tickets/{ticket.id}/restore-post", headers=admin_headers)
    assert response.status_code == 200
    assert len(publisher.sent) == 2


async def test_role_change_conflict_returns_ticket_ids(
    client, settings, session_factory, make_user, make_ticket
) -> None:
    admin = await make_user(telegram_id=550, role=UserRole.ADMIN)
    support = await make_user(telegram_id=551, role=UserRole.SUPPORT)
    developer = await make_user(telegram_id=552, role=UserRole.DEVELOPER)
    ticket = await make_ticket(author_id=support.id)
    async with session_factory() as session:
        await tickets_service.take_ticket(
            session, ticket_id=ticket.id, expected_version=1, actor_id=developer.id
        )

    headers = auth_headers(settings, 550)
    response = await client.patch(
        f"/api/users/{developer.id}/role", json={"role": None}, headers=headers
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "active_tickets",
        "ticket_ids": [ticket.id],
    }

    assert admin.id != developer.id


async def test_users_endpoints_are_admin_only(client, settings, make_user) -> None:
    await make_user(telegram_id=560, role=UserRole.DEVELOPER)
    await make_user(telegram_id=561, role=UserRole.ADMIN)

    response = await client.get("/api/users", headers=auth_headers(settings, 560))
    assert response.status_code == 403

    response = await client.get("/api/users", headers=auth_headers(settings, 561))
    assert response.status_code == 200
    assert response.json()["total"] >= 2

    response = await client.get("/api/users/assignable", headers=auth_headers(settings, 561))
    assert response.status_code == 200
    roles = {item["role"] for item in response.json()}
    assert roles <= {"developer", "admin"}


async def test_detail_not_found(client, settings, make_user) -> None:
    await make_user(telegram_id=570, role=UserRole.SUPPORT)
    response = await client.get("/api/tickets/99999", headers=auth_headers(settings, 570))
    assert response.status_code == 404


def _policy():
    from support_bot.config import Settings
    from support_bot.domain.reminders import ReminderPolicy

    return ReminderPolicy.from_settings(Settings())
