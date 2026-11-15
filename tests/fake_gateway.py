"""Recording fake of app.telegram_gateway.Gateway with scripted failures."""

import asyncio
from dataclasses import dataclass

from app.telegram_gateway import MessageNotFound


@dataclass
class Call:
    kind: str
    chat_id: int
    message_id: int | None
    text: str
    markup: object | None


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.texts: dict[int, str] = {}  # message_id -> last text
        self.live: set[int] = set()
        self._next_id = 1000
        self.errors: list[Exception] = []
        self.hold: asyncio.Event | None = None  # set to block the next call

    def script(self, *errors: Exception) -> None:
        """Next calls raise these errors in order; a plain Exception propagates."""
        self.errors.extend(errors)

    def _maybe_fail(self) -> None:
        if self.errors:
            raise self.errors.pop(0)

    async def _maybe_hold(self) -> None:
        if self.hold is not None:
            event, self.hold = self.hold, None
            await event.wait()

    async def send_message(self, chat_id: int, text: str, *, reply_markup: object = None) -> int:
        await self._maybe_hold()
        self._maybe_fail()
        message_id = self._next_id
        self._next_id += 1
        self.live.add(message_id)
        self.texts[message_id] = text
        self.calls.append(Call("send", chat_id, message_id, text, reply_markup))
        return message_id

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: object = None,
    ) -> None:
        await self._maybe_hold()
        self._maybe_fail()
        if message_id not in self.live:
            raise MessageNotFound("message to edit not found")
        self.texts[message_id] = text
        self.calls.append(Call("edit", chat_id, message_id, text, reply_markup))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self._maybe_fail()
        self.live.discard(message_id)
        self.texts.pop(message_id, None)
        self.calls.append(Call("delete", chat_id, message_id, "", None))

    def kinds(self) -> list[str]:
        return [c.kind for c in self.calls]
