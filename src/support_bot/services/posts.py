"""Serialize post delivery across replicas while keeping network calls outside transactions."""

import asyncio
from datetime import timedelta

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from structlog.contextvars import bound_contextvars
from telegram.error import RetryAfter, TelegramError

from support_bot.config import Settings
from support_bot.db.locks import ticket_lock
from support_bot.db.models import Ticket, User
from support_bot.observability.sentry import capture_error
from support_bot.telegram.transport import Transport

logger = structlog.get_logger(__name__)


class PostService:
    def __init__(
        self,
        engine: AsyncEngine,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
        transport: Transport,
    ):
        self.engine = engine
        self.sessions = sessions
        self.settings = settings
        self.transport = transport

    async def pending(self, limit: int | None = None) -> list[int]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(Ticket.id)
                    .where(
                        or_(
                            Ticket.message_id.is_(None), Ticket.post_synced_version < Ticket.version
                        )
                    )
                    .order_by(Ticket.id)
                    .limit(limit or self.settings.worker_batch_size)
                )
            )

    async def sync(self, ticket_id: int, force: bool = False, wait: bool = True) -> bool:
        async with ticket_lock(
            self.engine, ticket_id, self.settings.post_lock_timeout_seconds, wait
        ) as connection:
            if connection is None:
                return False
            for attempt in range(self.settings.telegram_retry_attempts):
                # New session per attempt prevents stale identity-map snapshots on retries.
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    ticket = await session.get(Ticket, ticket_id)
                    if ticket is None:
                        return False
                    if (
                        not force
                        and ticket.message_id is not None
                        and ticket.post_synced_version == ticket.version
                    ):
                        return False
                    author = await session.get(User, ticket.author_id)
                    assert author is not None
                    assignee = (
                        await session.get(User, ticket.assignee_id)
                        if ticket.assignee_id is not None
                        else None
                    )
                    rendered_version = ticket.version
                try:
                    message_id = await self.transport.publish(ticket, author, assignee, force=force)
                except TelegramError as error:
                    if attempt + 1 == self.settings.telegram_retry_attempts:
                        raise
                    delay = self.settings.telegram_retry_base_seconds * 2**attempt
                    if isinstance(error, RetryAfter):
                        retry_after = error.retry_after
                        delay = max(
                            delay,
                            retry_after.total_seconds()
                            if isinstance(retry_after, timedelta)
                            else float(retry_after),
                        )
                    await asyncio.sleep(delay)
                    continue
                async with (
                    AsyncSession(bind=connection, expire_on_commit=False) as session,
                    session.begin(),
                ):
                    await session.execute(
                        update(Ticket)
                        .where(Ticket.id == ticket_id)
                        .values(message_id=message_id, post_synced_version=rendered_version)
                    )
                return True
        return False

    async def reconcile(self) -> None:
        for ticket_id in await self.pending():
            try:
                await self.sync(ticket_id, wait=False)
            except Exception as error:
                logger.exception("post_sync_failed", ticket_id=ticket_id, component="posts")
                with bound_contextvars(ticket_id=ticket_id):
                    capture_error(error, component="posts")
