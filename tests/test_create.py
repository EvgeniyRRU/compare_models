"""Tests for the ``/new`` draft-form handlers (real PostgreSQL)."""

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telegram import CallbackQuery, ForceReply, Message, Update
from telegram.ext import ContextTypes

from app.bot.callbacks import encode_draft_menu_callback, encode_form_callback
from app.bot.handlers.create import (
    cancel_command,
    draft_menu_callback,
    draft_text_answer,
    form_callback,
    new_command,
)
from app.db.enums import DraftStep, Priority, Role
from app.db.models import Ticket, TicketDraft, User
from app.services.draft_service import set_prompt_message_id

CHAT_ID = 42


class FakeBot:
    """Records outgoing Telegram calls and returns minimal objects."""

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.answered_queries: list[dict[str, Any]] = []
        self._next_message_id = 1000

    async def send_message(
        self, chat_id: int, text: str, reply_markup: Any = None, **kwargs: Any
    ) -> Any:
        self._next_message_id += 1
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return SimpleNamespace(message_id=self._next_message_id)

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
def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _redirect_create(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
    create_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr("app.bot.handlers.create.async_session_factory", create_session_factory)
    monkeypatch.setattr("app.bot.handlers.create.engine", engine)


def make_chat() -> SimpleNamespace:
    return SimpleNamespace(type="private", id=CHAT_ID)


class FakeMessage:
    def __init__(self, text: str | None, reply_to: Any, replies: list[str]) -> None:
        self.text = text
        self.reply_to_message = reply_to
        self._replies = replies

    async def reply_text(self, text: str, **kwargs: Any) -> Message:
        self._replies.append(text)
        return cast(Message, SimpleNamespace(message_id=1))


def make_command_update(replies: list[str]) -> Update:
    message = cast(Message, FakeMessage(text="/new", reply_to=None, replies=replies))
    return cast(Update, SimpleNamespace(effective_message=message, effective_chat=make_chat()))


def make_text_update(text: str, reply_to: int | None, replies: list[str]) -> Update:
    reply = SimpleNamespace(message_id=reply_to) if reply_to is not None else None
    message = cast(Message, FakeMessage(text=text, reply_to=reply, replies=replies))
    return cast(Update, SimpleNamespace(effective_message=message, effective_chat=make_chat()))


def make_callback_query(bot: FakeBot, data: str) -> CallbackQuery:
    async def answer(
        text: str | None = None, show_alert: bool | None = None, **kwargs: Any
    ) -> bool:
        return await bot.answer_callback_query("query-id", text=text, show_alert=show_alert)

    return cast(CallbackQuery, SimpleNamespace(data=data, answer=answer))


def make_callback_update(bot: FakeBot, data: str) -> Update:
    query = make_callback_query(bot, data)
    return cast(Update, SimpleNamespace(callback_query=query, effective_chat=make_chat()))


def make_context(bot: FakeBot, user: User | None) -> ContextTypes.DEFAULT_TYPE:
    user_data: dict[str, Any] = {}
    if user is not None:
        user_data["user"] = user
    return cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace(user_data=user_data, bot=bot))


async def mk_user(session: AsyncSession, telegram_id: int, role: Role | None) -> User:
    user = User(telegram_id=telegram_id, role=role)
    session.add(user)
    await session.commit()
    return user


async def fresh_active_draft(session: AsyncSession, user_id: int) -> TicketDraft | None:
    stmt = (
        select(TicketDraft)
        .where(TicketDraft.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def fresh_draft(session: AsyncSession, draft_id: uuid.UUID) -> TicketDraft | None:
    stmt = (
        select(TicketDraft)
        .where(TicketDraft.id == draft_id)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalars().one_or_none()


async def fresh_ticket(session: AsyncSession, ticket_id: int) -> Ticket:
    stmt = select(Ticket).where(Ticket.id == ticket_id).execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalars().one()


async def test_happy_path_end_to_end(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    bot = FakeBot()
    replies: list[str] = []

    # /new -> description question (ForceReply, prompt recorded)
    await new_command(make_command_update(replies), make_context(bot, user))
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    assert draft.step is DraftStep.description
    assert draft.version == 1
    assert draft.prompt_message_id is not None
    assert isinstance(bot.sent_messages[-1]["reply_markup"], ForceReply)

    # answer description
    desc_prompt = draft.prompt_message_id
    await draft_text_answer(
        make_text_update("hello", desc_prompt, replies), make_context(bot, user)
    )
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    assert draft.step is DraftStep.priority
    assert draft.version == 2
    assert draft.description == "hello"
    assert draft.prompt_message_id is None

    # press priority high
    await form_callback(
        make_callback_update(
            bot, encode_form_callback("priority", draft.id, draft.version, Priority.high)
        ),
        make_context(bot, user),
    )
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    assert draft.step is DraftStep.request_id
    assert draft.version == 3
    assert draft.priority is Priority.high
    assert draft.prompt_message_id is not None

    # answer request_id
    req_prompt = draft.prompt_message_id
    await draft_text_answer(make_text_update("REQ-1", req_prompt, replies), make_context(bot, user))
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    assert draft.step is DraftStep.tenant_id
    assert draft.version == 4
    assert draft.request_id == "REQ-1"

    # press skip
    await form_callback(
        make_callback_update(bot, encode_form_callback("skip", draft.id, draft.version)),
        make_context(bot, user),
    )
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    assert draft.step is DraftStep.confirm
    assert draft.version == 5
    assert draft.tenant_id is None

    # press ok
    await form_callback(
        make_callback_update(bot, encode_form_callback("ok", draft.id, draft.version)),
        make_context(bot, user),
    )

    # draft gone, ticket created with correct fields
    assert await fresh_active_draft(session, user.id) is None
    tickets = (await session.execute(select(Ticket))).scalars().all()
    assert len(tickets) == 1
    ticket = await fresh_ticket(session, tickets[0].id)
    assert ticket.author_id == user.id
    assert ticket.description == "hello"
    assert ticket.priority is Priority.high
    assert ticket.request_id == "REQ-1"
    assert ticket.tenant_id is None
    assert ticket.version == 1
    assert ticket.message_id is not None  # post synced via FakeBot
    assert bot.answered_queries[-1]["text"] == "Обращение создано"


async def test_new_with_existing_draft_continue_and_restart(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    from app.services.draft_service import create_draft

    draft = await create_draft(session, user_id=user.id)
    await session.commit()
    draft_id = draft.id

    bot = FakeBot()
    replies: list[str] = []
    await new_command(make_command_update(replies), make_context(bot, user))
    assert any("незавершён" in r for r in replies)

    # continue re-asks the current step (description)
    await draft_menu_callback(
        make_callback_update(bot, encode_draft_menu_callback("continue", draft_id, 1)),
        make_context(bot, user),
    )
    f = await fresh_draft(session, draft_id)
    assert f is not None
    assert f.step is DraftStep.description
    assert f.prompt_message_id is not None

    # restart deletes the old draft and starts fresh
    await draft_menu_callback(
        make_callback_update(bot, encode_draft_menu_callback("restart", draft_id, 1)),
        make_context(bot, user),
    )
    assert await fresh_draft(session, draft_id) is None
    active = await fresh_active_draft(session, user.id)
    assert active is not None
    assert active.id != draft_id
    assert active.step is DraftStep.description
    assert active.version == 1


async def test_new_roleless_rejected(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, None)
    bot = FakeBot()
    replies: list[str] = []

    await new_command(make_command_update(replies), make_context(bot, user))

    assert any("администратор" in r for r in replies)
    assert await fresh_active_draft(session, user.id) is None
    assert bot.sent_messages == []


async def test_text_answer_not_bound_is_noop(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    bot = FakeBot()
    replies: list[str] = []

    await new_command(make_command_update(replies), make_context(bot, user))
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    prompt_id = draft.prompt_message_id
    assert prompt_id is not None

    # A message that isn't a reply must not mutate anything.
    await draft_text_answer(make_text_update("desc", None, replies), make_context(bot, user))
    f = await fresh_active_draft(session, user.id)
    assert f is not None
    assert f.step is DraftStep.description
    assert f.version == 1
    assert f.description is None

    # A reply to a different (stale) question must not mutate anything either.
    await draft_text_answer(
        make_text_update("desc", prompt_id + 999, replies), make_context(bot, user)
    )
    f = await fresh_active_draft(session, user.id)
    assert f is not None
    assert f.step is DraftStep.description
    assert f.version == 1
    assert f.description is None


async def test_stale_answer_does_not_land_in_next_field(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    bot = FakeBot()
    replies: list[str] = []

    await new_command(make_command_update(replies), make_context(bot, user))
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    desc_prompt = draft.prompt_message_id

    await draft_text_answer(make_text_update("desc", desc_prompt, replies), make_context(bot, user))
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    await form_callback(
        make_callback_update(
            bot, encode_form_callback("priority", draft.id, draft.version, Priority.high)
        ),
        make_context(bot, user),
    )
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    await draft_text_answer(
        make_text_update("REQ-1", draft.prompt_message_id, replies), make_context(bot, user)
    )
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    assert draft.step is DraftStep.tenant_id
    assert draft.version == 4
    assert draft.request_id == "REQ-1"

    # A re-sent answer replying to the OLD description question must be ignored.
    await draft_text_answer(
        make_text_update("INTRUDER", desc_prompt, replies), make_context(bot, user)
    )
    f = await fresh_active_draft(session, user.id)
    assert f is not None
    assert f.step is DraftStep.tenant_id
    assert f.version == 4
    assert f.request_id == "REQ-1"
    assert f.tenant_id is None


async def test_stale_form_callback_alerts_changed(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    bot = FakeBot()
    replies: list[str] = []

    await new_command(make_command_update(replies), make_context(bot, user))
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    await draft_text_answer(
        make_text_update("desc", draft.prompt_message_id, replies), make_context(bot, user)
    )
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    assert draft.version == 2
    assert draft.step is DraftStep.priority

    # Press a priority button with a stale version (1).
    await form_callback(
        make_callback_update(bot, encode_form_callback("priority", draft.id, 1, Priority.low)),
        make_context(bot, user),
    )

    f = await fresh_active_draft(session, user.id)
    assert f is not None
    assert f.version == 2
    assert f.step is DraftStep.priority
    assert f.priority is None
    assert bot.answered_queries[-1]["text"] == "Черновик устарел, начните заново"
    assert bot.answered_queries[-1]["show_alert"] is True


async def test_cancel_deletes_draft(session: AsyncSession) -> None:
    user = await mk_user(session, 1001, Role.support)
    bot = FakeBot()
    replies: list[str] = []

    await new_command(make_command_update(replies), make_context(bot, user))
    assert await fresh_active_draft(session, user.id) is not None

    await cancel_command(make_command_update(replies), make_context(bot, user))

    assert any("отменен" in r.lower() for r in replies)
    assert await fresh_active_draft(session, user.id) is None


async def test_late_prompt_send_is_noop(
    session: AsyncSession, create_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    user = await mk_user(session, 1001, Role.support)
    bot = FakeBot()
    replies: list[str] = []

    await new_command(make_command_update(replies), make_context(bot, user))
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    await draft_text_answer(
        make_text_update("desc", draft.prompt_message_id, replies), make_context(bot, user)
    )
    draft = await fresh_active_draft(session, user.id)
    assert draft is not None
    assert draft.version == 2
    assert draft.step is DraftStep.priority
    assert draft.prompt_message_id is None

    # A late prompt-send for the PREVIOUS step (description, version 1) is a no-op.
    async with create_session_factory() as fresh:
        ok = await set_prompt_message_id(
            fresh,
            draft_id=draft.id,
            expected_version=1,
            expected_step=DraftStep.description,
            message_id=99999,
        )
        await fresh.commit()
    assert ok is False

    f = await fresh_active_draft(session, user.id)
    assert f is not None
    assert f.prompt_message_id is None
