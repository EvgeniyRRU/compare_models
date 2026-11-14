"""Tests for the ``BotPostClient`` adapter (no database)."""

from types import SimpleNamespace
from typing import Any, cast

import pytest
from telegram import Bot
from telegram.error import BadRequest

from app.bot.post_client import BotPostClient
from app.services.post_sync import PostUnavailable


class FakeBot:
    """Raises the configured error from send/edit, otherwise returns a message id."""

    def __init__(
        self,
        *,
        send_error: BadRequest | None = None,
        edit_error: BadRequest | None = None,
    ) -> None:
        self.send_error = send_error
        self.edit_error = edit_error
        self.sent: list[tuple[str, Any | None]] = []
        self.edited: list[tuple[int | None, str, Any | None]] = []
        self._next_message_id = 100

    async def send_message(
        self, chat_id: int, text: str, reply_markup: Any = None, **kwargs: Any
    ) -> Any:
        if self.send_error is not None:
            raise self.send_error
        self._next_message_id += 1
        self.sent.append((text, reply_markup))
        return SimpleNamespace(message_id=self._next_message_id)

    async def edit_message_text(
        self,
        text: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        reply_markup: Any = None,
        **kwargs: Any,
    ) -> Any:
        if self.edit_error is not None:
            raise self.edit_error
        self.edited.append((message_id, text, reply_markup))
        return SimpleNamespace(message_id=message_id)


def make_client(bot: FakeBot) -> BotPostClient:
    return BotPostClient(cast(Bot, bot), chat_id=123)


async def test_send_post_delegates_and_returns_message_id() -> None:
    bot = FakeBot()
    client = make_client(bot)

    message_id = await client.send_post("hello", None)

    assert message_id == 101
    assert bot.sent == [("hello", None)]


async def test_edit_post_delegates_and_returns_message_id() -> None:
    bot = FakeBot()
    client = make_client(bot)

    message_id = await client.edit_post(55, "hello")

    assert message_id == 55
    assert bot.edited == [(55, "hello", None)]


async def test_edit_post_not_modified_is_success() -> None:
    bot = FakeBot(edit_error=BadRequest("Message is not modified: specified new message content"))
    client = make_client(bot)

    message_id = await client.edit_post(55, "same content")

    assert message_id == 55


async def test_edit_post_message_to_edit_not_found_raises_post_unavailable() -> None:
    bot = FakeBot(edit_error=BadRequest("Message to edit not found"))
    client = make_client(bot)

    with pytest.raises(PostUnavailable):
        await client.edit_post(55, "gone")


async def test_edit_post_other_bad_request_propagates() -> None:
    bot = FakeBot(edit_error=BadRequest("something else broke"))
    client = make_client(bot)

    with pytest.raises(BadRequest):
        await client.edit_post(55, "x")
