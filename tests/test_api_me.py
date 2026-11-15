"""Tests for the Mini App ``/api/me`` endpoint (real PostgreSQL)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.enums import Role
from app.db.models import User
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
    # ``get_session`` reads ``app.db.session.async_session_factory`` at call time.
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
    user = User(telegram_id=telegram_id, role=role, full_name="Alice")
    session.add(user)
    await session.flush()
    return user


async def test_me_returns_current_user_with_role(
    session: AsyncSession, client: httpx.AsyncClient
) -> None:
    await mk_user(session, 2001, Role.support)
    await session.commit()

    response = await client.get("/api/me", headers=auth_headers(2001))

    assert response.status_code == 200
    body = response.json()
    assert body["telegram_id"] == 2001
    assert body["role"] == "support"


async def test_me_roleless_user_returns_null_role(
    session: AsyncSession, client: httpx.AsyncClient
) -> None:
    await mk_user(session, 2001, None)
    await session.commit()

    response = await client.get("/api/me", headers=auth_headers(2001))

    assert response.status_code == 200
    assert response.json()["role"] is None


async def test_me_missing_header_401(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/me")

    assert response.status_code == 401
