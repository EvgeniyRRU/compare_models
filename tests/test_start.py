"""Tests for the ``/start`` and ``/help`` handlers (real PostgreSQL)."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import Message, Update
from telegram.ext import ContextTypes

from app.bot.handlers.start import help_command, start
from app.config import settings
from app.db.enums import Role
from app.db.models import User

BOOTSTRAP_ADMIN_ID = 4242


@pytest.fixture
def start_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the conftest test engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _redirect_start_session(
    monkeypatch: pytest.MonkeyPatch,
    start_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # ``start`` opens ``async_session_factory`` from its module namespace; point it
    # at the isolated test engine, and scope the bootstrap list to a known id.
    monkeypatch.setattr("app.bot.handlers.start.async_session_factory", start_session_factory)
    monkeypatch.setattr(settings, "bootstrap_admin_ids", [BOOTSTRAP_ADMIN_ID])


def make_update() -> tuple[Update, list[str]]:
    """Return an update whose ``effective_message.reply_text`` records the reply."""
    replies: list[str] = []

    async def reply_text(text: str, **kwargs: Any) -> Message:
        replies.append(text)
        return cast(Message, SimpleNamespace(message_id=1))

    message = cast(Message, SimpleNamespace(reply_text=reply_text))
    return cast(Update, SimpleNamespace(effective_message=message)), replies


def make_context(user: User | None = None) -> ContextTypes.DEFAULT_TYPE:
    user_data: dict[str, Any] = {}
    if user is not None:
        user_data["user"] = user
    return cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace(user_data=user_data))


def fresh_user_stmt(telegram_id: int) -> Select[User]:
    """Select a user bypassing the identity map (the handler commits in another session)."""
    return (
        select(User)
        .where(User.telegram_id == telegram_id)
        .execution_options(populate_existing=True)
    )


async def test_bootstrap_admin(session: AsyncSession) -> None:
    user = User(telegram_id=BOOTSTRAP_ADMIN_ID, full_name="Admin", role=None)
    session.add(user)
    await session.commit()

    update, replies = make_update()
    await start(update, make_context(user))

    row = (await session.scalars(fresh_user_stmt(BOOTSTRAP_ADMIN_ID))).one()
    assert row.role is Role.admin
    assert any("/help" in reply for reply in replies)


async def test_roleless_non_bootstrap_contacts_admin(session: AsyncSession) -> None:
    user = User(telegram_id=9999, full_name="Nobody", role=None)
    session.add(user)
    await session.commit()

    update, replies = make_update()
    await start(update, make_context(user))

    row = (await session.scalars(fresh_user_stmt(9999))).one()
    assert row.role is None
    assert any("администратор" in reply for reply in replies)


async def test_existing_role_not_overwritten(session: AsyncSession) -> None:
    # A support user whose telegram id is also in the bootstrap list keeps support.
    user = User(telegram_id=BOOTSTRAP_ADMIN_ID, full_name="Support", role=Role.support)
    session.add(user)
    await session.commit()

    update, replies = make_update()
    await start(update, make_context(user))

    row = (await session.scalars(fresh_user_stmt(BOOTSTRAP_ADMIN_ID))).one()
    assert row.role is Role.support
    assert any("/new" in reply for reply in replies)


async def test_help_lists_commands() -> None:
    update, replies = make_update()
    await help_command(update, make_context())

    assert any("/new" in reply and "/cancel" in reply for reply in replies)
