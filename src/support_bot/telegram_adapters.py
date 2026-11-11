from datetime import timedelta

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.error import RetryAfter as TgRetryAfter

from support_bot.services.post_sync import (
    Keyboard,
    PostUnavailable,
    RetryAfter,
    TransientTelegramError,
)


def _markup(keyboard: Keyboard) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in keyboard
        ]
    )


class TelegramChat:
    """Telegram operations for the developer chat. Implements the post and reminder senders."""

    def __init__(self, *, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send(self, text: str, keyboard: Keyboard) -> int:
        try:
            message = await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=_markup(keyboard),
            )
        except Exception as exc:
            raise _translate(exc) from exc
        return message.message_id

    async def edit(self, message_id: int, text: str, keyboard: Keyboard) -> None:
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=_markup(keyboard),
            )
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            raise _translate(exc) from exc
        except Exception as exc:
            raise _translate(exc) from exc

    async def remind(self, text: str, reply_to_message_id: int | None) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
                allow_sending_without_reply=True,
            )
        except Exception as exc:
            raise _translate(exc) from exc


def _translate(exc: Exception) -> Exception:
    if isinstance(exc, TgRetryAfter):
        delay = exc.retry_after
        return RetryAfter(delay.total_seconds() if isinstance(delay, timedelta) else float(delay))
    if isinstance(exc, BadRequest):
        lowered = str(exc).lower()
        if "not found" in lowered or "can't be edited" in lowered or "cannot be edited" in lowered:
            return PostUnavailable(str(exc))
        return exc
    if isinstance(exc, (NetworkError, TimedOut)):
        return TransientTelegramError(str(exc))
    return exc
