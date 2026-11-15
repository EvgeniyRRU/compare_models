"""Keep the dev-chat post in sync with the ticket.

One sync = one advisory lock per ticket on a dedicated (non-pooled) asyncpg
connection, then a fresh read + render inside that window. Because the text is
always re-rendered from the DB under the lock and post_synced_version is only
written after a successful publish/edit, a concurrent change simply leaves
post_synced_version behind and the sweeper re-syncs. The lock connection is
always closed at the end; if unlock misbehaves the close itself releases the
lock (the connection is discarded, never returned to a pool).
"""

import asyncio
import logging
from typing import Any

import asyncpg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import metrics
from app.bot.render import build_markup, render_post
from app.config import Settings
from app.db.models import Ticket, User
from app.telegram_gateway import Gateway, MessageNotFound

logger = logging.getLogger(__name__)

# Advisory-lock namespace; objid is the ticket id. Any stable constant works.
LOCK_NAMESPACE = 720_311


def asyncpg_dsn(database_url: str) -> str:
    """postgresql+asyncpg://... -> postgresql://... for raw asyncpg."""
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _acquire(conn: asyncpg.Connection, ticket_id: int) -> bool:
    return bool(
        await conn.fetchval("SELECT pg_try_advisory_lock($1, $2)", LOCK_NAMESPACE, ticket_id)
    )


async def _release(conn: asyncpg.Connection, ticket_id: int, locked: bool) -> None:
    try:
        if locked:
            unlocked = await conn.fetchval(
                "SELECT pg_advisory_unlock($1, $2)", LOCK_NAMESPACE, ticket_id
            )
            if not unlocked:
                logger.error("advisory unlock returned false for ticket %s", ticket_id)
    except Exception:
        logger.exception("advisory unlock failed for ticket %s", ticket_id)
    finally:
        # Closing discards the connection; if unlock failed, the lock dies with it.
        try:
            await conn.close()
        except Exception:
            logger.exception("closing lock connection failed")


async def _sync_under_lock(
    gateway: Gateway,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    ticket_id: int,
    forced: bool,
) -> None:
    async with session_factory() as session, session.begin():
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            raise LookupError(f"ticket {ticket_id} vanished")
        author = await session.get(User, ticket.author_id)
        assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
        text = render_post(ticket, author, assignee)
        markup = build_markup(ticket)
        message_id = ticket.message_id
        if forced or message_id is None:
            message_id = await gateway.send_message(settings.dev_chat_id, text, reply_markup=markup)
            result = "republished" if forced else "sent"
        else:
            try:
                await gateway.edit_message_text(
                    settings.dev_chat_id, message_id, text, reply_markup=markup
                )
                result = "edited"
            except MessageNotFound:
                message_id = await gateway.send_message(
                    settings.dev_chat_id, text, reply_markup=markup
                )
                result = "republished"
        ticket.message_id = message_id
        ticket.post_synced_version = ticket.version
    metrics.POST_SYNC_TOTAL.labels(result).inc()


async def sync_ticket(
    gateway: Gateway,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    ticket_id: int,
    *,
    forced: bool = False,
    lock_attempts: int = 3,
    lock_retry_seconds: float = 0.05,
) -> bool:
    """Sync one ticket post; returns False if the lock is busy or the ticket is gone."""
    conn: Any = await asyncpg.connect(asyncpg_dsn(settings.database_url))
    locked = False
    try:
        for _attempt in range(lock_attempts):
            locked = await _acquire(conn, ticket_id)
            if locked:
                break
            await asyncio.sleep(lock_retry_seconds)
        if not locked:
            metrics.POST_SYNC_TOTAL.labels("skipped").inc()
            logger.debug("post sync skipped (lock busy) for ticket %s", ticket_id)
            return False
        try:
            await _sync_under_lock(gateway, session_factory, settings, ticket_id, forced)
        except LookupError:
            logger.warning("ticket %s disappeared before sync", ticket_id)
            metrics.POST_SYNC_TOTAL.labels("gone").inc()
            return False
        return True
    except Exception:
        metrics.POST_SYNC_TOTAL.labels("error").inc()
        logger.exception("post sync failed for ticket %s", ticket_id)
        raise
    finally:
        release = asyncio.ensure_future(_release(conn, ticket_id, locked))
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            await release  # cleanup must finish even when the task is cancelled
            raise


async def sync_ticket_forced(
    gateway: Gateway,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    ticket_id: int,
) -> bool:
    """«Восстановить пост»: publish a fresh post and replace message_id."""
    return await sync_ticket(gateway, session_factory, settings, ticket_id, forced=True)
