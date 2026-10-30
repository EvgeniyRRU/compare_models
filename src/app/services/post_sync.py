import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import Ticket, User
from app.db.session import advisory_lock
from app.services.post_render import render_post
from app.services.publisher import MessageGone, Publisher

log = structlog.get_logger(component="post_sync")


class PostSyncer:
    def __init__(
        self,
        engine: AsyncEngine,
        sessionmaker: async_sessionmaker[AsyncSession],
        publisher: Publisher,
        chat_id: int,
        lock_timeout: float,
    ):
        self._engine = engine
        self._sm = sessionmaker
        self._publisher = publisher
        self._chat_id = chat_id
        self._lock_timeout = lock_timeout

    async def sync(self, ticket_id: int, *, force_new: bool = False, blocking: bool = True) -> bool:
        """Единая процедура публикации/правки поста. False — блокировка занята (blocking=False)."""
        async with advisory_lock(
            self._engine, ticket_id, self._lock_timeout, blocking=blocking
        ) as acquired:
            if not acquired:
                return False
            await self._sync_locked(ticket_id, force_new)
            return True

    async def _sync_locked(self, ticket_id: int, force_new: bool) -> None:
        # актуальное состояние читается под блокировкой, заранее подготовленный текст не используется
        async with self._sm() as session:
            ticket = await session.get(Ticket, ticket_id, populate_existing=True)
            if ticket is None:
                return
            author = await session.get(User, ticket.author_id)
            assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
            assert author is not None
            view = render_post(ticket, author, assignee)
            rendered_version = ticket.version
            message_id = None if force_new else ticket.message_id
        # транзакция на время запроса к Telegram не держится

        new_message_id: int | None = None
        if message_id is not None:
            try:
                await self._publisher.edit(self._chat_id, message_id, view)
            except MessageGone:
                log.warning("post_gone", ticket_id=ticket_id)
                message_id = None
        if message_id is None:
            new_message_id = await self._publisher.send(self._chat_id, view)

        values: dict[str, int] = {"post_synced_version": rendered_version}
        if new_message_id is not None:
            values["message_id"] = new_message_id
        async with self._sm() as session, session.begin():
            # техническая запись: version не меняется
            await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**values))
        log.info("post_synced", ticket_id=ticket_id, version=rendered_version)

    async def find_unsynced(self, limit: int = 50) -> list[int]:
        async with self._sm() as session:
            rows = await session.execute(
                select(Ticket.id)
                .where(
                    (Ticket.message_id.is_(None)) | (Ticket.post_synced_version < Ticket.version)
                )
                .order_by(Ticket.id)
                .limit(limit)
            )
            return list(rows.scalars())

    async def sync_pending(self) -> int:
        done = 0
        for ticket_id in await self.find_unsynced():
            try:
                if await self.sync(ticket_id, blocking=False):
                    done += 1
            except Exception:
                log.exception("post_sync_failed", ticket_id=ticket_id)
        return done
