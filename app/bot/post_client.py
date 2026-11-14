"""Adapter from a python-telegram-bot ``Bot`` to the :class:`PostClient` protocol."""

from typing import Any

from telegram import Bot
from telegram.error import BadRequest

from app.services.post_sync import PostUnavailable


class BotPostClient:
    """Publish/edit dev-chat posts through a python-telegram-bot ``Bot``.

    Implements the :class:`~app.services.post_sync.PostClient` protocol: ``send_post``
    publishes a new message and returns its ``message_id``, ``edit_post`` edits an
    existing message, treating Telegram's "message is not modified" as success and
    surfacing a deleted message as :class:`PostUnavailable`.
    """

    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = chat_id

    async def send_post(self, text: str, reply_markup: Any | None = None) -> int:
        message = await self._bot.send_message(
            chat_id=self._chat_id, text=text, reply_markup=reply_markup
        )
        return message.message_id

    async def edit_post(self, message_id: int, text: str, reply_markup: Any | None = None) -> int:
        try:
            message = await self._bot.edit_message_text(
                text=text,
                chat_id=self._chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
        except BadRequest as exc:
            reason = exc.message.lower()
            if "message is not modified" in reason:
                return message_id
            if "message to edit not found" in reason:
                raise PostUnavailable() from exc
            raise
        # ``edit_message_text`` returns ``True`` only for inline messages, which we
        # never use here; treat that case as "no new message id to return".
        if isinstance(message, bool):
            return message_id
        return message.message_id
