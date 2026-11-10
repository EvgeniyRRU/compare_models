from types import SimpleNamespace

from sqlalchemy import select

from app.bot.handlers import callback, identity, new, text_reply
from app.db.models import Ticket, User
from app.services.tickets import INTERVALS


async def test_full_draft_flow(sessions, monkeypatch):
    async with sessions.begin() as session:
        session.add(User(telegram_id=123, name="Support", role="support"))

    class FakeBot:
        def __init__(self):
            self.messages = []

        async def send_message(self, **kwargs):
            message = SimpleNamespace(message_id=len(self.messages) + 1, **kwargs)
            self.messages.append(message)
            return message

    class Message:
        def __init__(self, text=None, reply_id=None):
            self.text = text
            self.reply_to_message = SimpleNamespace(message_id=reply_id) if reply_id else None

        async def reply_text(self, text, **kwargs):
            await bot.send_message(text=text, chat_id=123, **kwargs)

    class Query:
        def __init__(self, data):
            self.data = data
            self.message = Message()

        async def answer(self, *args, **kwargs):
            pass

    async def no_sync(*args, **kwargs):
        pass

    monkeypatch.setattr("app.bot.handlers.sync_post", no_sync)
    bot = FakeBot()
    settings = SimpleNamespace(bootstrap_admin_ids=[], dev_chat_id=-1)
    context = SimpleNamespace(
        bot=bot,
        application=SimpleNamespace(
            bot_data={
                "sessions": sessions,
                "settings": settings,
                "engine": sessions.kw["bind"],
                "intervals": INTERVALS,
            }
        ),
    )
    person = SimpleNamespace(id=123, username="support", full_name="Support")
    chat = SimpleNamespace(type="private", id=123)

    def update(message=None, query=None):
        return SimpleNamespace(
            effective_user=person,
            effective_chat=chat,
            effective_message=message,
            message=message,
            callback_query=query,
        )

    await new(update(Message("/new")), context)
    prompt = bot.messages[-1]
    await text_reply(update(Message("A problem", prompt.message_id)), context)
    priority = bot.messages[-1].reply_markup.inline_keyboard[0][1].callback_data
    await callback(update(query=Query(priority)), context)
    prompt = bot.messages[-1]
    await text_reply(update(Message("request-1", prompt.message_id)), context)
    skip = bot.messages[-1].reply_markup.inline_keyboard[0][0].callback_data
    await callback(update(query=Query(skip)), context)
    submit = bot.messages[-1].reply_markup.inline_keyboard[0][0].callback_data
    await callback(update(query=Query(submit)), context)
    async with sessions() as session:
        ticket = await session.scalar(select(Ticket))
        assert ticket.description == "A problem"
        assert ticket.priority == "high"
        assert ticket.request_id == "request-1"
        assert ticket.tenant_id is None


async def test_bootstrap_admin_is_granted_only_on_first_start(sessions):
    context = SimpleNamespace(
        application=SimpleNamespace(
            bot_data={"sessions": sessions, "settings": SimpleNamespace(bootstrap_admin_ids=[123])}
        )
    )
    person = SimpleNamespace(id=123, username=None, full_name="Admin")

    def update(text):
        return SimpleNamespace(effective_user=person, message=SimpleNamespace(text=text))

    assert (await identity(update("hello"), context)).role is None
    assert (await identity(update("/start"), context)).role == "admin"
    async with sessions.begin() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 123))
        user.role = "support"
    assert (await identity(update("/start"), context)).role == "support"
