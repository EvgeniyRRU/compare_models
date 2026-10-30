import asyncio
from collections.abc import Awaitable, Callable

import structlog
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

from app.services.post_render import PostView
from app.services.publisher import MessageGone

log = structlog.get_logger(component="telegram")

_GONE_MARKERS = ("message to edit not found", "message can't be edited", "message_id_invalid")


def markup(view: PostView) -> InlineKeyboardMarkup | None:
    if not view.buttons:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(b.label, callback_data=b.callback_data) for b in row]
            for row in view.buttons
        ]
    )


async def with_retries[T](call: Callable[[], Awaitable[T]], attempts: int = 5) -> T:
    """Backoff с учётом retry_after при 429."""
    delay = 1.0
    for attempt in range(attempts):
        try:
            return await call()
        except RetryAfter as e:
            wait = float(
                e.retry_after.total_seconds()
                if hasattr(e.retry_after, "total_seconds")
                else e.retry_after
            )
            log.warning("telegram_retry_after", seconds=wait)
            await asyncio.sleep(wait + 0.5)
        except TimedOut, NetworkError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2
    raise RuntimeError("telegram retries exhausted")


class TelegramPublisher:
    def __init__(self, bot: Bot):
        self._bot = bot

    async def send(self, chat_id: int, view: PostView) -> int:
        msg = await with_retries(
            lambda: self._bot.send_message(
                chat_id,
                view.text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup(view),
                disable_web_page_preview=True,
            )
        )
        return msg.message_id

    async def edit(self, chat_id: int, message_id: int, view: PostView) -> None:
        try:
            await with_retries(
                lambda: self._bot.edit_message_text(
                    view.text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup(view),
                )
            )
        except BadRequest as e:
            text = str(e).lower()
            if "message is not modified" in text:
                return
            if any(m in text for m in _GONE_MARKERS):
                raise MessageGone from e
            raise

    async def reply(self, chat_id: int, message_id: int | None, text: str) -> None:
        params = (
            ReplyParameters(message_id=message_id, allow_sending_without_reply=True)
            if message_id
            else None
        )
        await self._bot.send_message(chat_id, text, reply_parameters=params)
