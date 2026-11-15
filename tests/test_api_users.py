"""Tests for the Mini App user endpoints (real PostgreSQL)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.enums import Priority, Role, TicketStatus
from app.db.models import Ticket, User
from app.main import create_app

from .api_helpers import auth_headers


@pytest.fixture
def api_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _redirect_api_db(
    monkeypatch: pytest.MonkeyPatch,
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The user routes only use ``get_session`` (the request-session dependency),
    # which reads ``app.db.session.async_session_factory`` at call time.
    monkeypatch.setattr("app.db.session.async_session_factory", api_session_factory)


@pytest.fixture
async def app(engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    yield create_app(engine=engine)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def mk_user(session: AsyncSession, telegram_id: int, role: Role | None = None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user


async def fresh_role(session: AsyncSession, user_id: int) -> Role | None:
    return await session.scalar(select(User.role).where(User.id == user_id))


async def test_list_users_admin(session: AsyncSession, client: httpx.AsyncClient) -> None:
    await mk_user(session, 2001, Role.support)
    await mk_user(session, 2003, Role.admin)
    await session.commit()

    response = await client.get("/api/users", headers=auth_headers(2003))

    assert response.status_code == 200
    roles = {u["telegram_id"]: u["role"] for u in response.json()}
    assert roles[2001] == "support"
    assert roles[2003] == "admin"


async def test_list_users_non_admin_403(session: AsyncSession, client: httpx.AsyncClient) -> None:
    await mk_user(session, 2001, Role.support)
    await session.commit()

    response = await client.get("/api/users", headers=auth_headers(2001))

    assert response.status_code == 403


async def test_set_role_happy_path(session: AsyncSession, client: httpx.AsyncClient) -> None:
    await mk_user(session, 2003, Role.admin)
    target = await mk_user(session, 2002, None)
    await session.commit()

    response = await client.put(
        f"/api/users/{target.id}/role",
        json={"role": "developer"},
        headers=auth_headers(2003),
    )

    assert response.status_code == 200
    assert response.json()["role"] == "developer"
    assert await fresh_role(session, target.id) is Role.developer


async def test_set_role_active_tickets_409(
    session: AsyncSession, client: httpx.AsyncClient
) -> None:
    admin = await mk_user(session, 2003, Role.admin)
    developer = await mk_user(session, 2002, Role.developer)
    await session.commit()
    ticket = Ticket(
        author_id=admin.id,
        assignee_id=developer.id,
        description="active",
        priority=Priority.medium,
        request_id="REQ-1",
        status=TicketStatus.in_progress,
        version=1,
        taken_at=datetime.now(UTC),
    )
    session.add(ticket)
    await session.flush()
    await session.commit()

    response = await client.put(
        f"/api/users/{developer.id}/role",
        json={"role": "support"},
        headers=auth_headers(2003),
    )

    assert response.status_code == 409
    assert response.json()["active_ticket_ids"] == [ticket.id]


async def test_set_role_non_admin_403(session: AsyncSession, client: httpx.AsyncClient) -> None:
    await mk_user(session, 2002, Role.developer)
    target = await mk_user(session, 2001, Role.support)
    await session.commit()

    response = await client.put(
        f"/api/users/{target.id}/role",
        json={"role": "admin"},
        headers=auth_headers(2002),
    )

    assert response.status_code == 403


async def test_set_role_unknown_user_404(session: AsyncSession, client: httpx.AsyncClient) -> None:
    await mk_user(session, 2003, Role.admin)
    await session.commit()

    response = await client.put(
        "/api/users/999999/role",
        json={"role": "developer"},
        headers=auth_headers(2003),
    )

    assert response.status_code == 404
