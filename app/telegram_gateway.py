"""Telegram sending layer.

TelegramGateway is a thin wrapper over python-telegram-bot's Bot with:
- backoff honoring Telegram's RetryAfter;
- "message is not modified" treated as success (no-op);
- "message to edit not found" surfaced as MessageNotFound so the caller can
  re-publish the post;
- send/edit return/accept message_id only.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Protocol

from telegram import Bot as PTBBot
from telegram import (
    ForceReply,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.error import BadRequest
from telegram.error import RetryAfter as TelegramRetryAfter

ReplyMarkup = InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | ForceReply

logger = logging.getLogger(__name__)


class RetryAfter(Exception):
    """Raised internally when Telegram asks to back off for N seconds."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry after {seconds}s")
        self.seconds = seconds


class MessageNotFound(Exception):
    """The target message does not exist anymore (deleted or never there)."""


def _seconds(exc: TelegramRetryAfter) -> float:
    value = exc.retry_after
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


def map_ptb_error(exc: BadRequest) -> Exception | None:
    """Translate a BadRequest into a domain error, or None for benign ones.

    None means "already in the desired state" — the edit/send can be considered
    successful.
    """
    message = str(exc).lower()
    if "message is not modified" in message:
        return None
    if "message to edit not found" in message or "message can't be edited" in message:
        return MessageNotFound(str(exc))
    return exc


async def call_with_backoff(
    fn: Callable[[], Awaitable],
    *,
    max_attempts: int = 5,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> object:
    """Run an async call, retrying RetryAfter errors with the requested delay."""
    attempt = 0
    while True:
        attempt += 1
        try:
            return await fn()
        except RetryAfter as exc:
            if attempt >= max_attempts:
                raise
            logger.warning("telegram retry_after=%.1s (attempt %s)", exc.seconds, attempt)
            await sleeper(exc.seconds)


class Gateway(Protocol):
    """What the bot needs from a Telegram sender (FakeGateway in tests)."""

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: ReplyMarkup | None = None,
        reply_to_message_id: int | None = None,
    ) -> int: ...

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None: ...

    async def delete_message(self, chat_id: int, message_id: int) -> None: ...

    async def answer_callback(
        self,
        callback_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None: ...


class TelegramGateway:
    """Gateway implementation on top of python-telegram-bot."""

    def __init__(self, token: str) -> None:
        self._bot = PTBBot(token=token)

    async def start(self) -> None:
        await self._bot.initialize()

    async def stop(self) -> None:
        await self._bot.shutdown()

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: ReplyMarkup | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        async def _call() -> int:
            try:
                msg = await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    reply_to_message_id=reply_to_message_id,
                    disable_web_page_preview=True,
                )
            except TelegramRetryAfter as exc:
                raise RetryAfter(_seconds(exc)) from exc
            except BadRequest as exc:
                translated = map_ptb_error(exc)
                if translated is None:
                    return 0  # pragma: no cover - send can't be "not modified"
                raise translated from exc
            return msg.message_id

        return await call_with_backoff(_call)  # type: ignore[return-value]

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        async def _call() -> None:
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=reply_markup,
                )
            except TelegramRetryAfter as exc:
                raise RetryAfter(_seconds(exc)) from exc
            except BadRequest as exc:
                translated = map_ptb_error(exc)
                if translated is None:
                    logger.debug("edit %s: message is not modified", message_id)
                    return
                raise translated from exc

        await call_with_backoff(_call)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        async def _call() -> None:
            try:
                await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            except TelegramRetryAfter as exc:
                raise RetryAfter(_seconds(exc)) from exc

        await call_with_backoff(_call)

    async def answer_callback(
        self,
        callback_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> None:
        async def _call() -> None:
            try:
                await self._bot.answer_callback_query(
                    callback_query_id=callback_id, text=text, show_alert=show_alert
                )
            except BadRequest as exc:
                # Expired/nonsense callbacks are harmless; the user already saw the UI.
                logger.debug("answer_callback failed: %s", exc)

        await call_with_backoff(_call)
