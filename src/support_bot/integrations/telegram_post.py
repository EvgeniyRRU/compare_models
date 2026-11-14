from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any, TypeVar

from telegram import Bot, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut

from support_bot.services.post_sync import PostNotFound

T = TypeVar("T")


class TelegramPostPublisher:
    def __init__(self, bot: Bot, *, max_attempts: int = 3) -> None:
        self._bot = bot
        self._max_attempts = max_attempts

    async def send_post(
        self, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None
    ) -> int:
        message = await self._retry(
            self._bot.send_message,
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return int(message.message_id)

    async def edit_post(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup | None,
    ) -> None:
        try:
            await self._retry(
                self._bot.edit_message_text,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            message = str(exc).lower()
            if "message is not modified" in message:
                return
            if "message to edit not found" in message or "message can't be edited" in message:
                raise PostNotFound(str(exc)) from exc
            raise

    async def send_reminder(self, chat_id: int, text: str, reply_to_message_id: int | None) -> None:
        """One attempt only: a lost reminder is acceptable by design."""
        await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_to_message_id=reply_to_message_id,
            allow_sending_without_reply=True,
            disable_web_page_preview=True,
        )

    async def _retry(self, func: Callable[..., Awaitable[T]], **kwargs: Any) -> T:
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await func(**kwargs)
            except RetryAfter as exc:
                if attempt >= self._max_attempts:
                    raise
                await asyncio.sleep(_retry_seconds(exc.retry_after) + 0.5)
            except TimedOut, NetworkError:
                if attempt >= self._max_attempts:
                    raise
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
            except TelegramError as exc:
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is None or attempt >= self._max_attempts:
                    raise
                await asyncio.sleep(_retry_seconds(retry_after) + 0.5)
        raise RuntimeError("unreachable")


def _retry_seconds(value: int | float | timedelta) -> float:
    if isinstance(value, timedelta):
        return value.total_seconds()
    return float(value)
