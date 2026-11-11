import json
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram import Update
from telegram.ext import Application

from support_bot.bot.app import build_application
from support_bot.config import Settings
from support_bot.db.models import Ticket, TicketDraft
from support_bot.domain.state_machine import Status
from support_bot.services import tickets as ticket_svc
from tests.fake_telegram import FakeTelegram
from tests.helpers import NOW, add_user

DEV_CHAT = -100


class Bot:
    def __init__(self, app: Application, fake: FakeTelegram) -> None:  # type: ignore[type-arg]
        self.app = app
        self.fake = fake
        self._update_id = 0

    async def send(self, update: dict[str, Any]) -> None:
        self._update_id += 1
        update["update_id"] = self._update_id
        await self.app.process_update(Update.de_json(update, self.app.bot))

    async def command(self, tg_id: int, text: str) -> None:
        await self.send(_message(tg_id, text))

    async def reply(self, tg_id: int, text: str, to_message: int) -> None:
        await self.send(_message(tg_id, text, reply_to=to_message))

    async def press(self, tg_id: int, data: str, on_message: int, chat: int | None = None) -> None:
        await self.send(
            {
                "callback_query": {
                    "id": f"cq{self._update_id}",
                    "from": _user(tg_id),
                    "chat_instance": "ci",
                    "data": data,
                    "message": _message_obj(on_message, chat or tg_id, "x"),
                }
            }
        )

    def last_markup(self) -> list[list[str]]:
        markup = self.fake.sent()[-1].get("reply_markup")
        if isinstance(markup, str):
            markup = json.loads(markup)
        return [[b["callback_data"] for b in row] for row in markup["inline_keyboard"]]

    def answers(self) -> list[dict[str, Any]]:
        return self.fake.sent("answerCallbackQuery")


def _user(tg_id: int) -> dict[str, Any]:
    return {"id": tg_id, "is_bot": False, "first_name": f"u{tg_id}", "username": f"u{tg_id}"}


def _message_obj(message_id: int, chat_id: int, text: str) -> dict[str, Any]:
    chat_type = "private" if chat_id > 0 else "supergroup"
    return {
        "message_id": message_id,
        "date": 0,
        "chat": {"id": chat_id, "type": chat_type},
        "text": text,
    }


def _message(tg_id: int, text: str, reply_to: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": 1,
        "date": 0,
        "chat": {"id": tg_id, "type": "private"},
        "from": _user(tg_id),
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if reply_to is not None:
        message["reply_to_message"] = _message_obj(reply_to, tg_id, "prompt")
    return {"message": message}


@pytest.fixture
async def bot(
    settings: Settings, engine: Any, session_factory: async_sessionmaker[AsyncSession]
) -> Any:
    fake = FakeTelegram()
    app = build_application(settings, engine, session_factory, request=fake)
    await app.initialize()
    yield Bot(app, fake)
    await app.shutdown()


def _bot_errors() -> float:
    return REGISTRY.get_sample_value("errors_total", {"component": "bot"}) or 0.0


async def test_user_without_role_is_told_to_ask_admin(bot: Bot, session: AsyncSession) -> None:
    await bot.command(9001, "/new")

    assert "администратор" in bot.fake.sent()[-1]["text"]


async def test_support_fills_form_and_creates_ticket_with_post(
    bot: Bot, session: AsyncSession
) -> None:
    await add_user(session, 5001, "support")
    errors_before = _bot_errors()

    await bot.command(5001, "/new")
    prompt = bot.fake.last_message_id
    assert "Опишите проблему" in bot.fake.sent()[-1]["text"]

    await bot.reply(5001, "Не открывается отчёт", to_message=prompt)
    priority_msg = bot.fake.last_message_id
    high = next(c for row in bot.last_markup() for c in row if c.endswith(":high"))
    await bot.press(5001, high, on_message=priority_msg)

    await bot.reply(5001, "REQ-42", to_message=bot.fake.last_message_id)
    await bot.reply(5001, "ответ не на вопрос", to_message=priority_msg)
    draft = (await session.scalars(select(TicketDraft))).one()
    await session.refresh(draft)
    assert draft.step == "tenant_id" and draft.request_id == "REQ-42"
    skip = next(c for row in bot.last_markup() for c in row if ":skip:" in c)
    await bot.press(5001, skip, on_message=bot.fake.last_message_id)

    confirm = [c for row in bot.last_markup() for c in row if ":ok:" in c]
    assert confirm, "confirmation step must show the send button"
    await bot.press(5001, confirm[0], on_message=bot.fake.last_message_id)

    ticket = (await session.scalars(select(Ticket))).one()
    assert ticket.description == "Не открывается отчёт"
    assert ticket.priority == "high"
    assert ticket.request_id == "REQ-42"
    assert ticket.status == "new"
    assert (await session.scalars(select(TicketDraft))).all() == []
    posts = [p for p in bot.fake.sent() if p.get("chat_id") == DEV_CHAT]
    assert posts and f"#{ticket.id}" in posts[-1]["text"]
    assert _bot_errors() == errors_before


async def test_developer_takes_ticket_by_post_button(
    bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 5010, "support")
    dev = await add_user(session, 5011, "developer")
    ticket = await ticket_svc.create_ticket(
        session,
        settings=settings,
        actor_id=support,
        description="Нет связи",
        priority="low",
        request_id="R",
        tenant_id=None,
        now=NOW,
    )
    await session.commit()
    runtime = bot.app.bot_data["runtime"]
    await runtime.sync_post(ticket.id)
    post_id = bot.fake.last_message_id

    await bot.press(5011, f"t:take:{ticket.id}:1", on_message=post_id, chat=DEV_CHAT)

    await session.refresh(ticket)
    assert ticket.status == "in_progress"
    assert ticket.assignee_id == dev
    assert any(c.get("chat_id") == DEV_CHAT for c in bot.fake.sent("editMessageText"))
    assert bot.answers()[-1].get("show_alert") is None


async def test_second_developer_gets_alert_for_taken_ticket(
    bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 5020, "support")
    await add_user(session, 5021, "developer")
    await add_user(session, 5022, "developer")
    ticket = await ticket_svc.create_ticket(
        session,
        settings=settings,
        actor_id=support,
        description="x",
        priority="low",
        request_id="R",
        tenant_id=None,
        now=NOW,
    )
    await session.commit()
    await bot.app.bot_data["runtime"].sync_post(ticket.id)
    post_id = bot.fake.last_message_id
    await bot.press(5021, f"t:take:{ticket.id}:1", on_message=post_id, chat=DEV_CHAT)

    await bot.press(5022, f"t:take:{ticket.id}:1", on_message=post_id, chat=DEV_CHAT)

    alert = bot.answers()[-1]
    assert alert["text"] == "Уже взято"
    assert alert["show_alert"] is True


async def test_old_button_after_status_cycle_is_rejected_and_post_resynced(
    bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 5030, "support")
    dev = await add_user(session, 5031, "developer")
    ticket = await ticket_svc.create_ticket(
        session,
        settings=settings,
        actor_id=support,
        description="x",
        priority="low",
        request_id="R",
        tenant_id=None,
        now=NOW,
    )
    await session.commit()
    await bot.app.bot_data["runtime"].sync_post(ticket.id)
    post_id = bot.fake.last_message_id
    await bot.press(5031, f"t:take:{ticket.id}:1", on_message=post_id, chat=DEV_CHAT)
    await bot.press(5031, f"t:wait:{ticket.id}:2", on_message=post_id, chat=DEV_CHAT)
    await bot.press(5031, f"t:work:{ticket.id}:3", on_message=post_id, chat=DEV_CHAT)
    edits_before = len(bot.fake.sent("editMessageText"))

    await bot.press(5031, f"t:wait:{ticket.id}:2", on_message=post_id, chat=DEV_CHAT)

    alert = bot.answers()[-1]
    assert alert["text"] == "Обращение изменилось"
    assert alert["show_alert"] is True
    assert len(bot.fake.sent("editMessageText")) > edits_before
    await session.refresh(ticket)
    assert ticket.status == Status.IN_PROGRESS.value and ticket.assignee_id == dev


async def test_admin_only_action_from_developer_is_refused(
    bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    support = await add_user(session, 5040, "support")
    await add_user(session, 5041, "developer")
    ticket = await ticket_svc.create_ticket(
        session,
        settings=settings,
        actor_id=support,
        description="x",
        priority="low",
        request_id="R",
        tenant_id=None,
        now=NOW,
    )
    await session.commit()
    await bot.app.bot_data["runtime"].sync_post(ticket.id)
    await bot.press(
        5041, f"t:take:{ticket.id}:1", on_message=bot.fake.last_message_id, chat=DEV_CHAT
    )

    await bot.press(
        5040, f"t:resolve:{ticket.id}:2", on_message=bot.fake.last_message_id, chat=DEV_CHAT
    )

    assert bot.answers()[-1]["show_alert"] is True
    await session.refresh(ticket)
    assert ticket.status == Status.IN_PROGRESS.value
