from __future__ import annotations

from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import joinedload
from telegram import InlineKeyboardMarkup

from support_bot.db.locks import advisory_lock
from support_bot.db.models.ticket import Ticket
from support_bot.db.session import SessionFactory
from support_bot.domain.errors import LockUnavailable
from support_bot.services.post_renderer import render_ticket_post


class PostNotFound(Exception):
    """The post was deleted or cannot be edited; publish a new one."""


class PostPublisher(Protocol):
    async def send_post(
        self, chat_id: int, text: str, keyboard: InlineKeyboardMarkup | None
    ) -> int: ...

    async def edit_post(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup | None,
    ) -> None: ...

    async def send_reminder(
        self, chat_id: int, text: str, reply_to_message_id: int | None
    ) -> None: ...


class PostSyncService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        publisher: PostPublisher,
        chat_id: int,
        database_dsn: str,
        lock_timeout: float = 10.0,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._chat_id = chat_id
        self._dsn = database_dsn
        self._lock_timeout = lock_timeout

    async def sync(
        self,
        ticket_id: int,
        *,
        force_new: bool = False,
        try_only: bool = False,
    ) -> bool:
        try:
            async with advisory_lock(
                self._dsn,
                ticket_id,
                wait_timeout=self._lock_timeout,
                try_only=try_only,
            ):
                return await self._sync_locked(ticket_id, force_new=force_new)
        except LockUnavailable:
            if try_only:
                return False
            raise

    async def _sync_locked(self, ticket_id: int, *, force_new: bool) -> bool:
        async with self._session_factory() as session:
            ticket = await session.scalar(
                select(Ticket)
                .options(joinedload(Ticket.author), joinedload(Ticket.assignee))
                .where(Ticket.id == ticket_id)
            )
            if ticket is None:
                return False
            text, keyboard = render_ticket_post(ticket)
            rendered_version = ticket.version
            message_id = ticket.message_id

        new_message_id: int | None = None
        if force_new or message_id is None:
            new_message_id = await self._publisher.send_post(self._chat_id, text, keyboard)
        else:
            try:
                await self._publisher.edit_post(self._chat_id, message_id, text, keyboard)
            except PostNotFound:
                new_message_id = await self._publisher.send_post(self._chat_id, text, keyboard)

        values: dict[str, object] = {"post_synced_version": rendered_version}
        if new_message_id is not None:
            values["message_id"] = new_message_id
        async with self._session_factory() as session, session.begin():
            await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**values))
        return True
