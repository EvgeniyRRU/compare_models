"""Telegram I/O only. Retry orchestration belongs to the post service."""

from typing import Protocol, TypedDict

from telegram import Bot, InlineKeyboardMarkup, ReplyParameters
from telegram.error import BadRequest

from support_bot.config import Settings
from support_bot.db.models import Ticket, User
from support_bot.telegram.rendering import render_post, ticket_keyboard


class _PostArguments(TypedDict):
    chat_id: int
    text: str
    reply_markup: InlineKeyboardMarkup
    read_timeout: float
    write_timeout: float
    connect_timeout: float
    parse_mode: None


class Transport(Protocol):
    async def publish(
        self, ticket: Ticket, author: User, assignee: User | None, force: bool = False
    ) -> int: ...

    async def send_reminder(self, ticket_id: int, message_id: int | None) -> None: ...


class TelegramTransport:
    def __init__(self, bot: Bot, settings: Settings):
        self.bot = bot
        self.settings = settings

    async def publish(
        self, ticket: Ticket, author: User, assignee: User | None, force: bool = False
    ) -> int:
        kwargs: _PostArguments = {
            "chat_id": self.settings.dev_chat_id,
            "text": render_post(ticket, author, assignee),
            "reply_markup": ticket_keyboard(ticket),
            "read_timeout": self.settings.telegram_timeout_seconds,
            "write_timeout": self.settings.telegram_timeout_seconds,
            "connect_timeout": self.settings.telegram_timeout_seconds,
            "parse_mode": None,
        }
        if ticket.message_id is not None and not force:
            try:
                await self.bot.edit_message_text(message_id=ticket.message_id, **kwargs)
                return ticket.message_id
            except BadRequest as exc:
                error = str(exc).lower()
                if "message is not modified" in error:
                    return ticket.message_id
                if not any(
                    fragment in error
                    for fragment in (
                        "message to edit not found",
                        "message can't be edited",
                        "message cannot be edited",
                    )
                ):
                    raise
        message = await self.bot.send_message(**kwargs)
        return message.message_id

    async def send_reminder(self, ticket_id: int, message_id: int | None) -> None:
        await self.bot.send_message(
            chat_id=self.settings.dev_chat_id,
            text=f"Напоминание: обращение #{ticket_id} ещё не взято в работу.",
            reply_parameters=ReplyParameters(
                message_id=message_id, allow_sending_without_reply=True
            )
            if message_id
            else None,
            read_timeout=self.settings.telegram_timeout_seconds,
            write_timeout=self.settings.telegram_timeout_seconds,
            connect_timeout=self.settings.telegram_timeout_seconds,
            parse_mode=None,
        )
