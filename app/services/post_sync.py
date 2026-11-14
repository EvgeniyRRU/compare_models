"""Post synchronization: publish/edit a ticket's dev-chat post under an advisory lock.

Business logic only — no Telegram or HTTP imports. The Telegram send/edit is
performed through the :class:`PostClient` protocol, which Stage 5 adapts to
python-telegram-bot.
"""

from collections.abc import Callable
from typing import Any, Protocol

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import settings
from app.db.models import Ticket, User
from app.db.session import advisory_lock
from app.services.render import render_post_text


class PostUnavailable(Exception):
    """The ticket's existing dev-chat post was deleted or is otherwise unavailable."""


class PostClient(Protocol):
    async def send_post(self, text: str, reply_markup: Any | None = None) -> int:
        """Publish a new post to the dev chat; return its Telegram message_id."""
        ...

    async def edit_post(self, message_id: int, text: str, reply_markup: Any | None = None) -> int:
        """Edit an existing post; return its message_id.

        Raise :class:`PostUnavailable` if the post was deleted. Treat Telegram's
        "message is not modified" as success (return the message_id).
        """
        ...


PostRenderer = Callable[[Ticket, User | None, User | None], tuple[str, Any | None]]


def render_ticket_post(
    ticket: Ticket, author: User | None, assignee: User | None
) -> tuple[str, Any | None]:
    """Render a post's (text, reply_markup); buttons arrive in a later stage."""
    return render_post_text(ticket, author=author, assignee=assignee), None


async def sync_ticket(
    *,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    ticket_id: int,
    client: PostClient,
    render: PostRenderer = render_ticket_post,
) -> None:
    """Synchronize one ticket's post, blocking on the advisory lock.

    Called immediately after a create/business-change commit.
    """
    await _sync_one(
        engine=engine,
        session_factory=session_factory,
        ticket_id=ticket_id,
        client=client,
        render=render,
        blocking=True,
    )


async def _sync_one(
    *,
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    ticket_id: int,
    client: PostClient,
    render: PostRenderer,
    blocking: bool,
) -> bool:
    """The single synchronization procedure; returns ``True`` when it synced.

    Steps: advisory-lock the ticket id on a dedicated connection, read + render
    the current state (remembering the version it was rendered from), send a new
    post or edit the existing one (re-sending if it was deleted), then persist
    ``post_synced_version`` and ``message_id`` in one transaction without
    bumping ``version``. The *rendered* version is persisted, not the current
    one, so a business change landing during the send leaves the ticket stale
    and eligible for re-sync.
    """
    async with advisory_lock(
        engine, ticket_id, timeout=settings.advisory_lock_timeout, blocking=blocking
    ) as lock_conn:
        if lock_conn is None:
            return False
        async with session_factory() as session:
            ticket = await session.get(Ticket, ticket_id, populate_existing=True)
            if ticket is None:
                return False
            author = await session.get(User, ticket.author_id)
            assignee = (
                await session.get(User, ticket.assignee_id)
                if ticket.assignee_id is not None
                else None
            )

            rendered_version = ticket.version
            text, reply_markup = render(ticket, author, assignee)

            if ticket.message_id is None:
                new_message_id = await client.send_post(text, reply_markup)
            else:
                try:
                    new_message_id = await client.edit_post(ticket.message_id, text, reply_markup)
                except PostUnavailable:
                    new_message_id = await client.send_post(text, reply_markup)

            await session.execute(
                update(Ticket)
                .where(Ticket.id == ticket_id)
                .values(
                    post_synced_version=rendered_version,
                    message_id=new_message_id,
                    # Explicit: a raw Core UPDATE bypasses the ORM ``onupdate``.
                    updated_at=func.now(),
                )
            )
            await session.commit()
            return True
