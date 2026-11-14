"""Tests for the ticket-action callback handler (real PostgreSQL)."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import CallbackQuery, Update
from telegram.ext import ContextTypes

from app.bot.callbacks import encode_ticket_callback
from app.bot.handlers.ticket_actions import ticket_action_callback
from app.db.enums import Priority, Role, TicketStatus
from app.db.models import Ticket, User
from app.services.state_machine import TicketAction
from app.services.ticket_service import create_ticket, take


class FakeBot:
    """Records outgoing Telegram calls and returns minimal objects."""

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.answered_queries: list[dict[str, Any]] = []
        self._next_message_id = 1000

    async def send_message(
        self, chat_id: int, text: str, reply_markup: Any = None, **kwargs: Any
    ) -> Any:
        self._next_message_id += 1
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return SimpleNamespace(message_id=self._next_message_id)

    async def edit_message_text(
        self,
        text: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.edited_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return SimpleNamespace(message_id=message_id)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool | None = None,
        **kwargs: Any,
    ) -> bool:
        self.answered_queries.append(
            {"callback_query_id": callback_query_id, "text": text, "show_alert": show_alert}
        )
        return True


@pytest.fixture
def ta_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _redirect_ticket_actions(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    ta_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The handler opens ``async_session_factory`` and passes ``engine`` to
    # ``sync_ticket``; point both at the isolated test engine.
    monkeypatch.setattr("app.bot.handlers.ticket_actions.async_session_factory", ta_session_factory)
    monkeypatch.setattr("app.bot.handlers.ticket_actions.engine", engine)


def make_callback_query(bot: FakeBot, data: str) -> CallbackQuery:
    async def answer(
        text: str | None = None, show_alert: bool | None = None, **kwargs: Any
    ) -> bool:
        return await bot.answer_callback_query("query-id", text=text, show_alert=show_alert)

    return cast(CallbackQuery, SimpleNamespace(data=data, answer=answer))


def make_update(query: CallbackQuery) -> Update:
    return cast(Update, SimpleNamespace(callback_query=query))


def make_context(bot: FakeBot, actor: User | None) -> ContextTypes.DEFAULT_TYPE:
    user_data: dict[str, Any] = {}
    if actor is not None:
        user_data["user"] = actor
    return cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace(user_data=user_data, bot=bot))


async def mk_user(session: AsyncSession, telegram_id: int, role: Role | None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.flush()
    return user


async def new_ticket(session: AsyncSession, author: User) -> Ticket:
    ticket = await create_ticket(
        session,
        author=author,
        priority=Priority.medium,
        description="a test ticket",
        request_id="REQ-1",
    )
    await session.commit()
    return ticket


async def fresh_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    stmt = select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalars().one()


async def test_take_transitions_to_in_progress(session: AsyncSession) -> None:
    author = await mk_user(session, 1001, Role.support)
    actor = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, author)

    bot = FakeBot()
    query = make_callback_query(bot, encode_ticket_callback(TicketAction.take, ticket.id, 1))
    await ticket_action_callback(make_update(query), make_context(bot, actor))

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.status is TicketStatus.in_progress
    assert fresh.assignee_id == actor.id
    assert fresh.version == 2
    assert len(bot.answered_queries) == 1
    assert bot.answered_queries[0]["show_alert"] is None
    assert len(bot.sent_messages) == 1


async def test_abandon_returns_to_new(session: AsyncSession) -> None:
    author = await mk_user(session, 1001, Role.support)
    actor = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, author)

    await take(session, ticket_id=ticket.id, actor=actor, expected_version=1)
    await session.commit()

    bot = FakeBot()
    query = make_callback_query(bot, encode_ticket_callback(TicketAction.abandon, ticket.id, 2))
    await ticket_action_callback(make_update(query), make_context(bot, actor))

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.status is TicketStatus.new
    assert fresh.assignee_id is None
    assert fresh.version == 3


async def test_resolve_finalizes(session: AsyncSession) -> None:
    author = await mk_user(session, 1001, Role.support)
    actor = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, author)

    await take(session, ticket_id=ticket.id, actor=actor, expected_version=1)
    await session.commit()

    bot = FakeBot()
    query = make_callback_query(bot, encode_ticket_callback(TicketAction.resolve, ticket.id, 2))
    await ticket_action_callback(make_update(query), make_context(bot, actor))

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.status is TicketStatus.resolved
    assert fresh.closed_at is not None
    assert fresh.version == 3


async def test_stale_version_alerts_changed(session: AsyncSession) -> None:
    author = await mk_user(session, 1001, Role.support)
    actor = await mk_user(session, 1002, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, author)

    await take(session, ticket_id=ticket.id, actor=actor, expected_version=1)
    await session.commit()

    bot = FakeBot()
    query = make_callback_query(bot, encode_ticket_callback(TicketAction.resolve, ticket.id, 1))
    await ticket_action_callback(make_update(query), make_context(bot, actor))

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.status is TicketStatus.in_progress
    assert fresh.version == 2
    assert len(bot.answered_queries) == 1
    assert bot.answered_queries[0]["text"] == "Обращение изменилось"
    assert bot.answered_queries[0]["show_alert"] is True


async def test_take_race_alerts_already_taken(session: AsyncSession) -> None:
    author = await mk_user(session, 1001, Role.support)
    taker = await mk_user(session, 1002, Role.developer)
    other = await mk_user(session, 1003, Role.developer)
    await session.commit()
    ticket = await new_ticket(session, author)

    await take(session, ticket_id=ticket.id, actor=taker, expected_version=1)
    await session.commit()

    bot = FakeBot()
    query = make_callback_query(bot, encode_ticket_callback(TicketAction.take, ticket.id, 1))
    await ticket_action_callback(make_update(query), make_context(bot, other))

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.status is TicketStatus.in_progress
    assert fresh.assignee_id == taker.id
    assert len(bot.answered_queries) == 1
    assert bot.answered_queries[0]["text"] == "Уже взято"
    assert bot.answered_queries[0]["show_alert"] is True


async def test_permission_denied(session: AsyncSession) -> None:
    author = await mk_user(session, 1001, Role.support)
    roleless = await mk_user(session, 1002, None)
    await session.commit()
    ticket = await new_ticket(session, author)

    bot = FakeBot()
    query = make_callback_query(bot, encode_ticket_callback(TicketAction.take, ticket.id, 1))
    await ticket_action_callback(make_update(query), make_context(bot, roleless))

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.status is TicketStatus.new
    assert fresh.assignee_id is None
    assert len(bot.answered_queries) == 1
    assert bot.answered_queries[0]["text"] == "Недостаточно прав"
    assert bot.answered_queries[0]["show_alert"] is True
    assert bot.sent_messages == []  # no re-sync on permission denial


async def test_malformed_callback_alerts(session: AsyncSession) -> None:
    author = await mk_user(session, 1001, Role.support)
    await session.commit()
    ticket = await new_ticket(session, author)

    bot = FakeBot()
    query = make_callback_query(bot, "not:a:callback")
    await ticket_action_callback(make_update(query), make_context(bot, None))

    fresh = await fresh_ticket(session, ticket.id)
    assert fresh.status is TicketStatus.new
    assert len(bot.answered_queries) == 1
    assert bot.answered_queries[0]["text"] == "Неверные данные кнопки"
    assert bot.answered_queries[0]["show_alert"] is True
    assert bot.sent_messages == []
