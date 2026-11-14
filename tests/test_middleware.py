"""Tests for the user-identification middleware (real PostgreSQL)."""

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import Update
from telegram import User as TgUser
from telegram.ext import ContextTypes

from app.bot.middleware import register_user_middleware
from app.db.enums import Role
from app.db.models import User


@pytest.fixture
def middleware_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the conftest test engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _redirect_middleware_session(
    monkeypatch: pytest.MonkeyPatch,
    middleware_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The middleware opens ``async_session_factory`` from its module namespace;
    # point it at the isolated test engine so rows land in ``support_test``.
    monkeypatch.setattr("app.bot.middleware.async_session_factory", middleware_session_factory)


def make_update(user: TgUser | None) -> Update:
    return cast(Update, SimpleNamespace(effective_user=user))


def make_context() -> tuple[ContextTypes.DEFAULT_TYPE, dict[str, Any]]:
    user_data: dict[str, Any] = {}
    return cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace(user_data=user_data)), user_data


async def test_creates_user_without_role(session: AsyncSession) -> None:
    update = make_update(TgUser(id=123, first_name="Alice", is_bot=False, username="alice"))
    context, user_data = make_context()

    await register_user_middleware(update, context)

    db_row = (await session.scalars(select(User).where(User.telegram_id == 123))).one()
    assert db_row.username == "alice"
    assert db_row.full_name == "Alice"
    assert db_row.role is None

    stored = user_data["user"]
    assert isinstance(stored, User)
    assert stored.id == db_row.id
    assert stored.role is None


async def test_updates_username_and_full_name(session: AsyncSession) -> None:
    session.add(User(telegram_id=123, username="old", full_name="Old Name", role=Role.support))
    await session.commit()

    update = make_update(
        TgUser(id=123, first_name="New", last_name="Name", is_bot=False, username="new_username")
    )
    context, _ = make_context()

    await register_user_middleware(update, context)

    row = (
        await session.execute(
            select(User.username, User.full_name, User.role).where(User.telegram_id == 123)
        )
    ).one()
    assert row.username == "new_username"
    assert row.full_name == "New Name"
    assert row.role is Role.support


async def test_unchanged_user_is_stable(session: AsyncSession) -> None:
    tg = TgUser(id=123, first_name="Alice", is_bot=False, username="alice")

    context1, _ = make_context()
    await register_user_middleware(make_update(tg), context1)

    context2, _ = make_context()
    await register_user_middleware(make_update(tg), context2)

    count = await session.scalar(select(func.count()).select_from(User))
    assert count == 1

    row = (
        await session.execute(select(User.username, User.full_name).where(User.telegram_id == 123))
    ).one()
    assert row.username == "alice"
    assert row.full_name == "Alice"


async def test_noop_without_effective_user(session: AsyncSession) -> None:
    context, user_data = make_context()

    await register_user_middleware(make_update(None), context)

    count = await session.scalar(select(func.count()).select_from(User))
    assert count == 0
    assert "user" not in user_data


async def test_concurrent_first_time_registration_is_single_row(session: AsyncSession) -> None:
    tg1 = TgUser(id=999, first_name="Alice", is_bot=False, username="alice")
    tg2 = TgUser(id=999, first_name="Alice", is_bot=False, username="alice")

    context1, _ = make_context()
    context2, _ = make_context()

    # Two concurrent first-time updates for a brand-new user must not race:
    # the upsert collapses them into a single row without raising.
    await asyncio.gather(
        register_user_middleware(make_update(tg1), context1),
        register_user_middleware(make_update(tg2), context2),
    )

    rows = (await session.scalars(select(User).where(User.telegram_id == 999))).all()
    assert len(rows) == 1
