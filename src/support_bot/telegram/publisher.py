"""Реализация PostPublisher поверх Bot API: ретраи с backoff и учётом retry_after."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog
from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter

from support_bot.services.ports import Keyboard, PostGoneError

log = structlog.get_logger(__name__)

_NOT_MODIFIED = "message is not modified"
_GONE_MARKERS = (
    "message to edit not found",
    "message can't be edited",
    "message_id_invalid",
    "message not found",
)

type Sleep = Callable[[float], Awaitable[None]]


def to_markup(keyboard: Keyboard) -> InlineKeyboardMarkup | None:
    if not keyboard:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(b.text, callback_data=b.callback_data) for b in row]
            for row in keyboard
        ]
    )


def _seconds(value: int | float | timedelta) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


async def call_with_retry[T](
    call: Callable[[], Awaitable[T]],
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
    sleep: Sleep = asyncio.sleep,
) -> T:
    """Повторять сетевые ошибки и 429; BadRequest/Forbidden не повторяются."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await call()
        except BadRequest:
            raise
        except RetryAfter as exc:
            if attempt >= max_attempts:
                raise
            delay = _seconds(exc.retry_after) + 0.5
        except NetworkError:  # включая TimedOut
            if attempt >= max_attempts:
                raise
            delay = min(base_delay * 2 ** (attempt - 1), max_delay)
        log.warning("telegram_retry", attempt=attempt, delay=delay)
        await sleep(delay)


class TelegramPublisher:
    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._retry = {
            "max_attempts": max_attempts,
            "base_delay": base_delay,
            "max_delay": max_delay,
            "sleep": sleep,
        }

    async def _call[T](self, call: Callable[[], Awaitable[T]]) -> T:
        return await call_with_retry(call, **self._retry)  # type: ignore[arg-type]

    async def send_post(self, text: str, keyboard: Keyboard) -> int:
        # Если результат неизвестен (таймаут), повтор может дать редкий дубль поста — допустимо.
        message: Message = await self._call(
            lambda: self._bot.send_message(
                self._chat_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=to_markup(keyboard),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        )
        return message.message_id

    async def edit_post(self, message_id: int, text: str, keyboard: Keyboard) -> None:
        try:
            await self._call(
                lambda: self._bot.edit_message_text(
                    text,
                    chat_id=self._chat_id,
                    message_id=message_id,
                    parse_mode=ParseMode.HTML,
                    reply_markup=to_markup(keyboard),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            )
        except BadRequest as exc:
            msg = exc.message.lower()
            if _NOT_MODIFIED in msg:
                return
            if any(marker in msg for marker in _GONE_MARKERS):
                raise PostGoneError(exc.message) from exc
            raise

    async def send_reminder(self, text: str, reply_to_message_id: int | None) -> None:
        # Одна попытка на срок: без ретраев.
        reply = (
            ReplyParameters(message_id=reply_to_message_id, allow_sending_without_reply=True)
            if reply_to_message_id is not None
            else None
        )
        await self._bot.send_message(self._chat_id, text, reply_parameters=reply)
