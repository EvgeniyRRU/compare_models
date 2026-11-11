import asyncio
import json
from typing import Any

from telegram.request import BaseRequest


class FakeTelegram(BaseRequest):
    """Telegram Bot API transport double: records calls, returns plausible results."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._message_id = 1000

    @property
    def read_timeout(self) -> float | None:
        return None

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def do_request(  # type: ignore[override]
        self,
        url: str,
        method: str,
        request_data: Any = None,
        **kwargs: Any,
    ) -> tuple[int, bytes]:
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((name, params))
        if name == "getUpdates":
            # Stand-in for long polling, so the polling loop does not spin.
            await asyncio.sleep(0.01)
        return 200, json.dumps({"ok": True, "result": self._result(name, params)}).encode()

    def _result(self, name: str, params: dict[str, Any]) -> Any:
        if name == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "Bot", "username": "support_test_bot"}
        if name in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            if name == "sendMessage":
                self._message_id += 1
            chat_id = int(params.get("chat_id", 0))
            return {
                "message_id": self._message_id,
                "date": 0,
                "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
                "text": params.get("text", ""),
            }
        if name == "getUpdates":
            return []
        return True

    def sent(self, name: str = "sendMessage") -> list[dict[str, Any]]:
        return [params for method, params in self.calls if method == name]

    @property
    def last_message_id(self) -> int:
        return self._message_id
