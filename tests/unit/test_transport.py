from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, RetryAfter

from support_bot.db.models import Ticket, User
from support_bot.telegram.transport import TelegramTransport


def entities():
    return Ticket(
        id=1,
        message_id=5,
        version=1,
        description="Test",
        status="new",
        request_id="R",
        priority="low",
    ), User(first_name="Автор", telegram_id=1, username=None)


async def test_not_modified_is_success_and_deleted_falls_back(settings):
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified")),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=6)),
    )
    transport = TelegramTransport(bot, settings)
    ticket, author = entities()
    assert await transport.publish(ticket, author, None) == 5
    bot.send_message.assert_not_called()
    bot.edit_message_text.side_effect = BadRequest("Message to edit not found")
    assert await transport.publish(ticket, author, None) == 6
    assert bot.send_message.call_args.kwargs["chat_id"] == settings.dev_chat_id


async def test_retry_after_propagates_and_force_sends_new(settings):
    bot = SimpleNamespace(
        edit_message_text=AsyncMock(side_effect=RetryAfter(2)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=6)),
    )
    transport = TelegramTransport(bot, settings)
    ticket, author = entities()
    with pytest.raises(RetryAfter):
        await transport.publish(ticket, author, None)
    assert await transport.publish(ticket, author, None, force=True) == 6
    await transport.send_reminder(1, 6)
    assert bot.send_message.call_args.kwargs["reply_parameters"].message_id == 6
    assert bot.send_message.call_args.kwargs["chat_id"] == settings.dev_chat_id
