"""Синхронизация поста в чате разработчиков с состоянием обращения в БД.

Источник истины — БД. Одна процедура для первой публикации и всех правок:
advisory lock по id обращения → чтение актуального состояния → рендер → send/edit →
запись post_synced_version (= отрендеренной version) и message_id → снятие блокировки.
"""

from dataclasses import dataclass
from enum import StrEnum

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from support_bot.db.advisory_lock import LockNotAcquiredError, advisory_lock
from support_bot.db.engine import SessionFactory
from support_bot.db.models import Ticket
from support_bot.domain.errors import NotFoundError, PermissionDeniedError
from support_bot.domain.state_machine import is_admin
from support_bot.monitoring import metrics
from support_bot.rendering import render_post
from support_bot.services.ports import PostGoneError, PostPublisher
from support_bot.services.users import load_actor
from support_bot.services.views import load_ticket_view

log = structlog.get_logger(__name__)


class SyncResult(StrEnum):
    SENT = "sent"
    EDITED = "edited"
    SKIPPED = "skipped"  # блокировка занята: синхронизирует владелец или фоновая задача
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class SyncOutcome:
    result: SyncResult
    rendered_version: int | None = None
    message_id: int | None = None


class PostSyncService:
    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: SessionFactory,
        publisher: PostPublisher,
        *,
        lock_timeout: float,
        batch_size: int = 50,
    ) -> None:
        self._engine = engine
        self._sf = session_factory
        self._publisher = publisher
        self._lock_timeout = lock_timeout
        self._batch_size = batch_size

    async def sync(
        self, ticket_id: int, *, wait: bool = True, force_new: bool = False
    ) -> SyncOutcome:
        """Синхронизировать пост. wait=False — не ждать занятую блокировку."""
        timeout = self._lock_timeout if wait else 0.0
        try:
            async with advisory_lock(self._engine, ticket_id, timeout=timeout):
                outcome = await self._sync_locked(ticket_id, force_new=force_new)
        except LockNotAcquiredError:
            outcome = SyncOutcome(SyncResult.SKIPPED)
        except Exception:
            metrics.POST_SYNCS.labels(result="failed").inc()
            raise
        metrics.POST_SYNCS.labels(result=outcome.result.value).inc()
        return outcome

    async def sync_safely(self, ticket_id: int, *, wait: bool = True) -> SyncOutcome | None:
        """Синхронизация после коммита: ошибка логируется, повторит фоновая задача."""
        try:
            return await self.sync(ticket_id, wait=wait)
        except Exception:
            log.exception("post_sync_failed", ticket_id=ticket_id)
            metrics.ERRORS.labels(component="post_sync").inc()
            return None

    async def restore(self, actor_id: int, ticket_id: int) -> SyncOutcome:
        """«Восстановить пост» (admin): принудительно опубликовать новый пост."""
        async with self._sf() as session:
            actor = await load_actor(session, actor_id)
            if not is_admin(actor.role):
                raise PermissionDeniedError()
        outcome = await self.sync(ticket_id, wait=True, force_new=True)
        if outcome.result is SyncResult.NOT_FOUND:
            raise NotFoundError("Обращение не найдено")
        return outcome

    async def pending_ids(self, limit: int | None = None) -> list[int]:
        async with self._sf() as session:
            rows = await session.scalars(
                select(Ticket.id)
                .where(
                    or_(Ticket.message_id.is_(None), Ticket.post_synced_version < Ticket.version)
                )
                .order_by(Ticket.id)
                .limit(limit or self._batch_size)
            )
            return list(rows)

    async def sync_pending(self) -> int:
        """Фоновая задача: опубликовать/обновить отставшие посты; занятые пропустить."""
        done = 0
        for ticket_id in await self.pending_ids():
            with structlog.contextvars.bound_contextvars(ticket_id=ticket_id):
                outcome = await self.sync_safely(ticket_id, wait=False)
            if outcome is not None and outcome.result in (SyncResult.SENT, SyncResult.EDITED):
                done += 1
        return done

    async def _sync_locked(self, ticket_id: int, *, force_new: bool) -> SyncOutcome:
        async with self._sf() as session:
            view = await load_ticket_view(session, ticket_id)
        if view is None:
            return SyncOutcome(SyncResult.NOT_FOUND)

        # Рендерим только из только что прочитанного состояния, запоминая его version.
        post = render_post(view)
        new_message_id: int | None = None
        if force_new or view.message_id is None:
            new_message_id = await self._publisher.send_post(post.text, post.keyboard)
        else:
            try:
                await self._publisher.edit_post(view.message_id, post.text, post.keyboard)
            except PostGoneError:
                log.info("post_gone_republishing", ticket_id=ticket_id, message_id=view.message_id)
                new_message_id = await self._publisher.send_post(post.text, post.keyboard)

        values: dict[str, int] = {"post_synced_version": post.version}
        if new_message_id is not None:
            values["message_id"] = new_message_id
        async with self._sf.begin() as session:
            # Техническая запись: version не меняется.
            await session.execute(update(Ticket).where(Ticket.id == ticket_id).values(**values))

        result = SyncResult.SENT if new_message_id is not None else SyncResult.EDITED
        log.info("post_synced", ticket_id=ticket_id, version=post.version, result=result.value)
        return SyncOutcome(result, post.version, new_message_id or view.message_id)
