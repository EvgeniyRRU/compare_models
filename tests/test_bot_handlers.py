"""Сквозные тесты хендлеров: настоящие PTB Update, подменённый Bot без сети."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from telegram import Bot, Chat, Message, Update

from support_bot.callbacks import TicketCallback
from support_bot.config import Settings
from support_bot.container import Container, build_container
from support_bot.db.models import Ticket
from support_bot.domain.enums import Role, TicketStatus
from support_bot.domain.state_machine import Action
from support_bot.telegram.handlers import BotHandlers
from tests.conftest import FakePublisher, MakeUser


class FakeBot(Bot):
    def __init__(self) -> None:
        super().__init__(token="123456:TEST-token")
        # Bot «заморожен»: публичные атрибуты задавать нельзя, только с подчёркиванием.
        self._messages: list[SimpleNamespace] = []
        self._answers: list[tuple[str | None, bool]] = []
        self._next_id = 500

    @property
    def messages(self) -> list[SimpleNamespace]:
        return self._messages

    @property
    def answers(self) -> list[tuple[str | None, bool]]:
        return self._answers

    async def send_message(self, chat_id: Any, text: str, *args: Any, **kwargs: Any) -> Message:
        self._next_id += 1
        self._messages.append(
            SimpleNamespace(id=self._next_id, chat_id=chat_id, text=text, **kwargs)
        )
        return Message(self._next_id, datetime.now(UTC), Chat(chat_id, Chat.PRIVATE), text=text)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        self._answers.append((text, bool(show_alert)))
        return True

    async def edit_message_reply_markup(self, *args: Any, **kwargs: Any) -> bool:
        return True

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def last(self) -> SimpleNamespace:
        return self.messages[-1]

    def last_markup(self) -> Any:
        for m in reversed(self.messages):
            if getattr(m, "reply_markup", None) is not None:
                return m.reply_markup
        raise AssertionError("no markup")


class Harness:
    def __init__(self, container: Container, bot: FakeBot) -> None:
        self.h = BotHandlers(container)
        self.bot = bot
        self.ctx = SimpleNamespace(bot=bot)
        self.uid = 0

    def _user(self, tg_id: int) -> dict[str, Any]:
        return {"id": tg_id, "is_bot": False, "first_name": f"U{tg_id}"}

    def message(self, tg_id: int, text: str, reply_to: int | None = None) -> Update:
        self.uid += 1
        msg: dict[str, Any] = {
            "message_id": 10_000 + self.uid,
            "date": 0,
            "chat": {"id": tg_id, "type": "private"},
            "from": self._user(tg_id),
            "text": text,
        }
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        if reply_to is not None:
            msg["reply_to_message"] = {
                "message_id": reply_to,
                "date": 0,
                "chat": {"id": tg_id, "type": "private"},
            }
        return Update.de_json({"update_id": self.uid, "message": msg}, self.bot)

    def callback(self, tg_id: int, data: str, chat_id: int | None = None) -> Update:
        self.uid += 1
        chat = chat_id if chat_id is not None else tg_id
        return Update.de_json(
            {
                "update_id": self.uid,
                "callback_query": {
                    "id": str(self.uid),
                    "from": self._user(tg_id),
                    "chat_instance": "x",
                    "data": data,
                    "message": {
                        "message_id": 900,
                        "date": 0,
                        "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
                    },
                },
            },
            self.bot,
        )


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def harness(
    settings: Settings, engine: AsyncEngine, publisher: FakePublisher, bot: FakeBot
) -> Harness:
    return Harness(build_container(settings, engine, publisher), bot)


def button_data(markup: Any, text_part: str) -> str:
    for row in markup.inline_keyboard:
        for b in row:
            if text_part in b.text:
                return str(b.callback_data)
    raise AssertionError(f"button {text_part!r} not found")


async def test_full_form_through_handlers(
    harness: Harness,
    bot: FakeBot,
    publisher: FakePublisher,
    make_user: MakeUser,
    engine: AsyncEngine,
) -> None:
    support = await make_user(Role.SUPPORT)
    tg = support.telegram_id
    h, ctx = harness.h, harness.ctx

    await h.cmd_new(harness.message(tg, "/new"), ctx)
    q1 = bot.last()
    assert "Шаг 1" in q1.text

    # Ответ не reply-ем: данные не меняются, вопрос задаётся заново.
    await h.on_private_message(harness.message(tg, "без привязки"), ctx)
    q1b = bot.last()
    assert "Шаг 1" in q1b.text and "Ответьте" in q1b.text

    # Ответ на старый вопрос после повторного показа — тоже не принимается.
    await h.on_private_message(harness.message(tg, "старый", reply_to=q1.id), ctx)
    assert "Шаг 1" in bot.last().text
    q1c = bot.last()

    await h.on_private_message(harness.message(tg, "Не работает оплата", reply_to=q1c.id), ctx)
    assert "Шаг 2" in bot.last().text
    await h.on_draft_button(harness.callback(tg, button_data(bot.last_markup(), "Высокий")), ctx)
    q3 = bot.last()
    assert "Шаг 3" in q3.text
    await h.on_private_message(harness.message(tg, "REQ-7", reply_to=q3.id), ctx)
    skip_msg = bot.last()
    assert "Нет id арендатора" in skip_msg.text
    await h.on_draft_button(
        harness.callback(tg, button_data(skip_msg.reply_markup, "Пропустить")), ctx
    )
    confirm = bot.last()
    assert "Проверьте обращение" in confirm.text and "REQ-7" in confirm.text
    submit = button_data(confirm.reply_markup, "Отправить")
    await h.on_draft_button(harness.callback(tg, submit), ctx)
    assert bot.answers[-1][0] is not None and "создано" in bot.answers[-1][0]
    assert len(publisher.sent) == 1 and "Не работает оплата" in publisher.sent[0][1]

    # Повторное «Отправить» ничего не создаёт.
    await h.on_draft_button(harness.callback(tg, submit), ctx)
    assert bot.answers[-1][1] is True
    async with engine.connect() as conn:
        assert await conn.scalar(select(func.count()).select_from(Ticket)) == 1


async def test_developer_cannot_start_form(
    harness: Harness, bot: FakeBot, make_user: MakeUser
) -> None:
    dev = await make_user(Role.DEVELOPER)
    await harness.h.cmd_new(harness.message(dev.telegram_id, "/new"), harness.ctx)
    assert "только support и admin" in bot.last().text


async def test_start_without_role(harness: Harness, bot: FakeBot) -> None:
    await harness.h.cmd_start(harness.message(4242, "/start"), harness.ctx)
    assert "Обратитесь к администратору" in bot.last().text


async def test_ticket_buttons(
    harness: Harness,
    bot: FakeBot,
    publisher: FakePublisher,
    make_user: MakeUser,
    engine: AsyncEngine,
    settings: Settings,
) -> None:
    support, d1, d2 = (
        await make_user(Role.SUPPORT),
        await make_user(Role.DEVELOPER),
        await make_user(Role.DEVELOPER),
    )
    c = harness.h.c
    from support_bot.domain.enums import Priority
    from support_bot.domain.validation import TicketData

    tid = await c.tickets.create(support.id, TicketData("desc", Priority.LOW, "R", None))
    await c.post_sync.sync(tid)
    take = TicketCallback(Action.TAKE, tid, 1).encode()
    chat = settings.dev_chat_id

    await harness.h.on_ticket_button(harness.callback(d1.telegram_id, take, chat), harness.ctx)
    assert bot.answers[-1] == ("Готово", False)
    await harness.h.on_ticket_button(harness.callback(d2.telegram_id, take, chat), harness.ctx)
    assert bot.answers[-1] == ("Уже взято", True)

    # support не может брать
    await harness.h.on_ticket_button(harness.callback(support.telegram_id, take, chat), harness.ctx)
    assert bot.answers[-1][1] is True

    # чужой developer не может решить
    resolve = TicketCallback(Action.RESOLVE, tid, 2).encode()
    await harness.h.on_ticket_button(harness.callback(d2.telegram_id, resolve, chat), harness.ctx)
    assert bot.answers[-1] == ("Недостаточно прав для этого действия", True)

    await harness.h.on_ticket_button(harness.callback(d1.telegram_id, resolve, chat), harness.ctx)
    assert bot.answers[-1] == ("Готово", False)
    view = await c.tickets.get_view(tid)
    assert view is not None and view.status is TicketStatus.RESOLVED
    assert view.post_synced_version == view.version
    (message_id,) = publisher.posts
    assert "Решено" in publisher.posts[message_id][0]
    assert publisher.posts[message_id][1] == ()

    # старая кнопка → «Обращение изменилось»
    await harness.h.on_ticket_button(harness.callback(d1.telegram_id, resolve, chat), harness.ctx)
    assert bot.answers[-1] == ("Обращение изменилось", True)
