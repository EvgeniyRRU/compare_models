"""Тестовые двойники: RecordingBot и сборщики апдейтов/initData."""

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import quote

from telegram import Bot, Chat, Message, User


class RecordingBot(Bot):
    """PTB-бот без сетевых вызовов: записывает все действия."""

    def __init__(self, token: str):
        super().__init__(token)
        self._frozen = (
            False  # PTB замораживает объект в Bot.__init__; тесты переприсваивают поля в рантайме
        )
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []
        self.commands: list[Any] | None = None
        self.webhook: dict[str, Any] | None = None
        self.fail_next_send: Exception | None = None
        self.fail_next_edit: Exception | None = None
        self.send_hook: Any = None  # async callable — пауза для тестов конкурентности

    async def initialize(self, *args: Any, **kwargs: Any) -> None:
        self._bot_user = await self.get_me()

    async def shutdown(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get_me(self, *args: Any, **kwargs: Any) -> User:
        return User(id=123456789, is_bot=True, first_name="Test Bot", username="test_bot")

    async def set_my_commands(self, commands: Any, *args: Any, **kwargs: Any) -> bool:
        self.commands = commands
        return True

    async def set_webhook(self, url: str, **kwargs: Any) -> bool:
        self.webhook = {"url": url, **kwargs}
        return True

    async def delete_webhook(self, **kwargs: Any) -> bool:
        return True

    async def send_message(self, chat_id: Any, text: str, **kwargs: Any) -> Message:
        if self.fail_next_send is not None:
            exc, self.fail_next_send = self.fail_next_send, None
            raise exc
        if self.send_hook is not None:
            await self.send_hook()
        self.sent.append(
            {"chat_id": chat_id, "message_id": len(self.sent) + 1, "text": text, **kwargs}
        )
        return Message(
            message_id=len(self.sent),
            date=int(time.time()),
            chat=Chat(id=chat_id, type="private"),
            text=text,
        )

    async def edit_message_text(
        self, text: str, chat_id: Any = None, message_id: int | None = None, **kwargs: Any
    ) -> bool:
        if self.fail_next_edit is not None:
            exc, self.fail_next_edit = self.fail_next_edit, None
            raise exc
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text, **kwargs})
        return True

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
        **kwargs: Any,
    ) -> bool:
        self.answered.append({"id": callback_query_id, "text": text, "show_alert": show_alert})
        return True


def make_user_dict(
    user_id: int = 111, username: str | None = None, first_name: str = "Test"
) -> dict:
    d: dict[str, Any] = {"id": user_id, "is_bot": False, "first_name": first_name}
    if username:
        d["username"] = username
    return d


def make_update(
    update_id: int,
    user_id: int = 111,
    *,
    text: str | None = None,
    message_id: int = 1,
    reply_to_message_id: int | None = None,
    data: str | None = None,
) -> dict:
    """dict для Update.de_json: сообщение (text/reply) либо callback_query."""
    user = make_user_dict(user_id)
    if data is not None:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb{update_id}",
                "from": user,
                "chat_instance": f"ci{update_id}",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": 1700000000,
                    "chat": {"id": user_id, "type": "private"},
                    "from": user,
                    "text": "question",
                },
            },
        }
    message: dict[str, Any] = {
        "message_id": message_id,
        "date": 1700000000,
        "chat": {"id": user_id, "type": "private"},
        "from": user,
        "text": text if text is not None else "",
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "date": 1699999999,
            "chat": {"id": user_id, "type": "private"},
            "from": make_user_dict(123456789, first_name="Bot"),
            "text": "question",
        }
    if text is not None and text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return {"update_id": update_id, "message": message}


def make_init_data(
    user_id: int,
    token: str,
    *,
    username: str | None = None,
    first_name: str = "Test",
    age_seconds: int = 0,
) -> str:
    """Валидный initData Mini App (HMAC-SHA256 по документации Telegram)."""
    payload: dict[str, Any] = {
        "id": user_id,
        "is_bot": False,
        "first_name": first_name,
        "last_name": "User",
    }
    if username:
        payload["username"] = username
    params = {"user": json.dumps(payload, separators=(",", ":"))}
    # auth_date всегда присутствует (даже при age_seconds=0): validate_init_data требует его
    params["auth_date"] = str(int(time.time()) - age_seconds)
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(token.encode(), b"WebAppData", hashlib.sha256).digest()
    digest = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return (
        "&".join(f"{k}={quote(v, safe='')}" for k, v in sorted(params.items())) + f"&hash={digest}"
    )
