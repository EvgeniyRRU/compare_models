"""Exercise real PTB dispatch across two replicas, without contacting Telegram."""

import json
import time
from contextlib import AsyncExitStack

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from telegram.ext import ExtBot
from telegram.request import BaseRequest

from support_bot.db.models import Ticket, TicketDraft, User
from support_bot.main import create_app
from support_bot.services.drafts import DraftService
from support_bot.services.posts import PostService
from support_bot.services.tickets import TicketService
from support_bot.services.users import UserService
from support_bot.telegram.application import build_application
from support_bot.telegram.transport import TelegramTransport

pytestmark = pytest.mark.integration


class TelegramRequest(BaseRequest):
    def __init__(self):
        self.message_id = 100
        self.calls = []

    @property
    def read_timeout(self):
        return 10

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        operation = url.rsplit("/", 1)[-1]
        params = request_data.json_parameters if request_data else {}
        self.calls.append((operation, params))
        if operation == "getMe":
            result = {
                "id": 123456,
                "is_bot": True,
                "first_name": "Support",
                "username": "support_test_bot",
            }
        elif operation == "sendMessage":
            self.message_id += 1
            chat_id = int(params["chat_id"])
            result = {
                "message_id": self.message_id,
                "date": int(time.time()),
                "text": params["text"],
                "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


async def test_form_across_replicas_submits_once_and_publishes(db, settings):
    settings.bot_mode = "webhook"
    settings.webhook_secret = SecretStr("test_secret")
    async with db.sessions.begin() as session:
        session.add(User(telegram_id=42, first_name="Support", role="support"))
    request = TelegramRequest()
    apps = []
    for _ in range(2):
        bot = ExtBot(settings.bot_token.get_secret_value(), request=request)
        users = UserService(db.sessions, settings)
        tickets = TicketService(db.sessions, settings)
        drafts = DraftService(db.sessions, settings)
        posts = PostService(db.engine, db.sessions, settings, TelegramTransport(bot, settings))
        ptb = build_application(settings, users, tickets, drafts, posts, bot=bot)
        apps.append(create_app(settings, database=db, application=ptb, enable_workers=False))
    identity = {"id": 42, "is_bot": False, "first_name": "Support"}
    chat = {"id": 42, "type": "private"}
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret"}
    async with AsyncExitStack() as stack:
        for app in apps:
            await stack.enter_async_context(app.router.lifespan_context(app))
        clients = [
            await stack.enter_async_context(
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
            )
            for app in apps
        ]
        update_id = 0

        async def send_message(value, reply=None):
            nonlocal update_id
            update_id += 1
            message = {
                "message_id": update_id,
                "date": int(time.time()),
                "chat": chat,
                "from": identity,
                "text": value,
            }
            if value.startswith("/"):
                message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(value)}]
            if reply is not None:
                message["reply_to_message"] = {
                    "message_id": reply,
                    "date": int(time.time()),
                    "chat": chat,
                }
            result = await clients[update_id % 2].post(
                "/telegram/webhook",
                json={"update_id": update_id, "message": message},
                headers=headers,
            )
            assert result.status_code == 200

        async def current_draft():
            async with db.sessions() as session:
                return await session.scalar(select(TicketDraft))

        async def click(action):
            nonlocal update_id
            draft = await current_draft()
            update_id += 1
            payload = {
                "update_id": update_id,
                "callback_query": {
                    "id": str(update_id),
                    "from": identity,
                    "chat_instance": "c",
                    "data": f"d:{draft.id.hex}:{draft.version}:{action}",
                    "message": {
                        "message_id": request.message_id,
                        "date": int(time.time()),
                        "chat": chat,
                    },
                },
            }
            response = await clients[update_id % 2].post(
                "/telegram/webhook", json=payload, headers=headers
            )
            assert response.status_code == 200
            return payload

        await send_message("/new")
        draft = await current_draft()
        assert draft and draft.prompt_message_id is not None
        await send_message("Description", draft.prompt_message_id)
        await click("phigh")
        draft = await current_draft()
        await send_message("REQUEST-42", draft.prompt_message_id)
        await click("skip")
        submission = await click("submit")
        await clients[0].post("/telegram/webhook", json=submission, headers=headers)
        assert await current_draft() is None
        async with db.sessions() as session:
            saved = list(await session.scalars(select(Ticket)))
            assert len(saved) == 1
            assert saved[0].request_id == "REQUEST-42"
            assert saved[0].message_id is not None and saved[0].post_synced_version == 1
        posts = [
            params
            for operation, params in request.calls
            if operation == "sendMessage" and int(params["chat_id"]) == settings.dev_chat_id
        ]
        assert len(posts) == 1
