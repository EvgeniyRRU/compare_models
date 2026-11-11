import pytest
from telegram import InlineKeyboardMarkup
from telegram.error import BadRequest, NetworkError
from telegram.error import RetryAfter as TgRetryAfter

from support_bot.services.post_sync import (
    PostUnavailable,
    RetryAfter,
    TransientTelegramError,
)
from support_bot.telegram_adapters import TelegramChat


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.error: Exception | None = None
        self.next_message_id = 500

    async def send_message(self, **kwargs: object) -> object:
        self.calls.append(("send", kwargs))
        if self.error:
            raise self.error
        self.next_message_id += 1

        class Sent:
            message_id = self.next_message_id

        return Sent()

    async def edit_message_text(self, **kwargs: object) -> object:
        self.calls.append(("edit", kwargs))
        if self.error:
            raise self.error
        return True


def _chat(bot: FakeBot) -> TelegramChat:
    return TelegramChat(bot=bot, chat_id=-100)  # type: ignore[arg-type]


async def test_send_returns_message_id_and_attaches_keyboard() -> None:
    bot = FakeBot()

    message_id = await _chat(bot).send("текст", [[("Взять", "t:take:1:1")]])

    assert message_id == 501
    kind, kwargs = bot.calls[0]
    assert kind == "send" and kwargs["chat_id"] == -100
    assert isinstance(kwargs["reply_markup"], InlineKeyboardMarkup)


async def test_edit_with_same_text_counts_as_success() -> None:
    bot = FakeBot()
    bot.error = BadRequest("Message is not modified: specified new message content")

    await _chat(bot).edit(7, "текст", [])


async def test_edit_of_missing_message_means_post_unavailable() -> None:
    bot = FakeBot()
    bot.error = BadRequest("Message to edit not found")

    with pytest.raises(PostUnavailable):
        await _chat(bot).edit(7, "текст", [])


async def test_rate_limit_becomes_retry_after() -> None:
    bot = FakeBot()
    bot.error = TgRetryAfter(12)

    with pytest.raises(RetryAfter) as info:
        await _chat(bot).send("x", [])

    assert info.value.seconds == 12


async def test_network_error_is_transient() -> None:
    bot = FakeBot()
    bot.error = NetworkError("connection reset")

    with pytest.raises(TransientTelegramError):
        await _chat(bot).send("x", [])


async def test_reminder_replies_to_post_without_failing_when_post_is_gone() -> None:
    bot = FakeBot()

    await _chat(bot).remind("напоминание", 42)

    kwargs = bot.calls[0][1]
    assert kwargs["reply_to_message_id"] == 42
    assert kwargs["allow_sending_without_reply"] is True
