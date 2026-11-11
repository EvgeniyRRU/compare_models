import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Protocol

import structlog
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.db.models import Ticket, User
from support_bot.domain.errors import NotFound
from support_bot.domain.state_machine import Status
from support_bot.services.post_render import (
    BUTTON_TITLES,
    PostView,
    actions_for,
    callback_data,
    render_post,
)

LOCK_NAMESPACE = 7001
log = structlog.get_logger()

Keyboard = list[list[tuple[str, str]]]


class LockTimeout(Exception):
    """Another replica is working on this ticket's post."""


class PostUnavailable(Exception):
    """The post was deleted or cannot be edited any more; publish a new one."""


class RetryAfter(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"retry after {seconds}s")
        self.seconds = seconds


class TransientTelegramError(Exception):
    """Network or server error that is safe to retry."""


class PostSender(Protocol):
    async def send(self, text: str, keyboard: Keyboard) -> int: ...

    async def edit(self, message_id: int, text: str, keyboard: Keyboard) -> None: ...


@asynccontextmanager
async def advisory_lock(
    engine: AsyncEngine, ticket_id: int, timeout_seconds: float
) -> AsyncIterator[None]:
    """Session-level advisory lock on a dedicated connection.

    The lock is released in `finally`. If the release fails, the connection is
    invalidated so the lock cannot leak back into the pool.
    """
    conn = await engine.connect()
    locked = False
    try:
        deadline = time.monotonic() + timeout_seconds
        while True:
            locked = bool(
                await conn.scalar(
                    text("SELECT pg_try_advisory_lock(:ns, :id)"),
                    {"ns": LOCK_NAMESPACE, "id": ticket_id},
                )
            )
            if locked:
                break
            if time.monotonic() >= deadline:
                raise LockTimeout(f"ticket {ticket_id} is busy")
            await asyncio.sleep(0.05)
        yield
    finally:
        if locked:
            await _release(conn, ticket_id)
        await conn.close()


async def _release(conn: AsyncConnection, ticket_id: int) -> None:
    try:
        released = await conn.scalar(
            text("SELECT pg_advisory_unlock(:ns, :id)"),
            {"ns": LOCK_NAMESPACE, "id": ticket_id},
        )
    except BaseException:
        await conn.invalidate()
        raise
    if not released:
        await conn.invalidate()


async def _load_view(session: AsyncSession, ticket_id: int) -> tuple[PostView, int, int | None]:
    ticket = await session.get(Ticket, ticket_id, populate_existing=True)
    if ticket is None:
        raise NotFound("Обращение не найдено")
    author = await session.get(User, ticket.author_id)
    assignee = await session.get(User, ticket.assignee_id) if ticket.assignee_id else None
    view = PostView(
        ticket_id=ticket.id,
        version=ticket.version,
        status=Status(ticket.status),
        priority=ticket.priority,
        author=_display_name(author),
        assignee=_display_name(assignee) if assignee else None,
        request_id=ticket.request_id,
        tenant_id=ticket.tenant_id,
        description=ticket.description,
        created_at=ticket.created_at,
    )
    return view, ticket.version, ticket.message_id


def _display_name(user: User | None) -> str:
    if user is None:
        return "—"
    return f"@{user.username}" if user.username else user.full_name


def _keyboard(view: PostView) -> Keyboard:
    buttons = [
        (
            BUTTON_TITLES[action],
            callback_data(action, ticket_id=view.ticket_id, version=view.version),
        )
        for action in actions_for(view.status)
    ]
    return [buttons] if buttons else []


async def _retry[T](op: Callable[[], Awaitable[T]], *, attempts: int, base_seconds: float) -> T:
    for attempt in range(attempts):
        try:
            return await op()
        except RetryAfter as exc:
            delay = exc.seconds
        except TransientTelegramError:
            delay = base_seconds * (2**attempt)
        if attempt == attempts - 1:
            raise TransientTelegramError("retries exhausted")
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


async def sync_ticket(
    engine: AsyncEngine,
    *,
    settings: Settings,
    sender: PostSender,
    ticket_id: int,
    lock_timeout_seconds: float | None = None,
    retry_base_seconds: float = 0.5,
    max_attempts: int = 5,
) -> None:
    """Bring the ticket's post in line with the database.

    Runs under the advisory lock. The transaction is not held open while talking
    to Telegram: the state is read, the post is sent, then the result is saved.
    """
    timeout = (
        settings.post_sync_lock_timeout_seconds
        if lock_timeout_seconds is None
        else lock_timeout_seconds
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with advisory_lock(engine, ticket_id, timeout):
        async with factory() as session:
            view, rendered_version, message_id = await _load_view(session, ticket_id)
        text_body = render_post(view)
        keyboard = _keyboard(view)

        async def send() -> int:
            return await sender.send(text_body, keyboard)

        async def edit(target: int) -> None:
            await sender.edit(target, text_body, keyboard)

        retry = partial(_retry, attempts=max_attempts, base_seconds=retry_base_seconds)
        new_message_id: int
        if message_id is None:
            new_message_id = await retry(send)
        else:
            try:
                await retry(partial(edit, message_id))
                new_message_id = message_id
            except PostUnavailable:
                new_message_id = await retry(send)

        async with factory.begin() as session:
            await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket_id)
                .values(message_id=new_message_id, post_synced_version=rendered_version)
            )


async def find_out_of_sync(session: AsyncSession, limit: int = 50) -> list[int]:
    rows = await session.scalars(
        select(Ticket.id)
        .where((Ticket.message_id.is_(None)) | (Ticket.post_synced_version < Ticket.version))
        .order_by(Ticket.id)
        .limit(limit)
    )
    return list(rows)


async def sync_pending(
    engine: AsyncEngine,
    *,
    settings: Settings,
    sender: PostSender,
    limit: int = 50,
    retry_base_seconds: float = 0.5,
) -> int:
    """Background pass: sync every out-of-date post whose lock is free right now."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        ids = await find_out_of_sync(session, limit)

    synced = 0
    for ticket_id in ids:
        try:
            await sync_ticket(
                engine,
                settings=settings,
                sender=sender,
                ticket_id=ticket_id,
                lock_timeout_seconds=0,
                retry_base_seconds=retry_base_seconds,
            )
        except LockTimeout:
            continue
        except Exception:
            log.exception("post sync failed", component="post_sync", ticket_id=ticket_id)
            continue
        synced += 1
    return synced
