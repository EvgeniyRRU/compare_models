"""Ticket-draft service: business logic for the multi-step ``/new`` form.

Pure business logic — no Telegram or HTTP imports. Every operation takes an
``AsyncSession`` (the caller manages the transaction boundary) and follows the
same optimistic-concurrency contract as :mod:`app.services.ticket_service`:

* a step mutation is a single conditional ``UPDATE`` guarded by
  ``WHERE id = :id AND version = :expected_version AND step = :expected_step``,
  bumping ``version`` exactly once and clearing ``prompt_message_id``;
* if the conditional ``UPDATE`` matched zero rows, nothing changed and the
  operation raises :class:`DraftChanged` (or :class:`DraftNotFound` if the row
  no longer exists).

Helpers never commit: the caller (or a thin wrapper) owns the commit.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.enums import DraftStep, Priority
from app.db.models import Ticket, TicketDraft, User
from app.services.ticket_service import create_ticket


class DraftNotFound(Exception):
    """The draft no longer exists."""

    def __init__(self, draft_id: uuid.UUID) -> None:
        self.draft_id = draft_id
        super().__init__("Черновик не найден")


class DraftChanged(Exception):
    """The draft's version/step no longer matches the caller's expectation."""

    def __init__(self, draft_id: uuid.UUID) -> None:
        self.draft_id = draft_id
        super().__init__("Черновик устарел")


class DraftValidationError(Exception):
    """The submitted value failed validation (message is the Russian display text)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _now() -> datetime:
    return datetime.now(UTC)


async def get_active_draft(session: AsyncSession, user_id: int) -> TicketDraft | None:
    """Return the user's in-progress draft, if any (at most one per user)."""
    return await session.scalar(select(TicketDraft).where(TicketDraft.user_id == user_id))


async def get_draft(session: AsyncSession, draft_id: uuid.UUID) -> TicketDraft | None:
    return await session.get(TicketDraft, draft_id)


async def create_draft(session: AsyncSession, *, user_id: int) -> TicketDraft:
    """Insert a new draft at ``description`` and return it (flushed so the UUID id is populated).

    The caller is expected to have already checked there is no active draft for
    this user; the ``UNIQUE(user_id)`` constraint backs that check up.
    """
    draft = TicketDraft(user_id=user_id, step=DraftStep.description, version=1)
    session.add(draft)
    await session.flush()
    return draft


async def delete_draft(session: AsyncSession, *, draft_id: uuid.UUID) -> bool:
    """Delete a draft by id; return whether a row was deleted."""
    deleted_id = await session.scalar(
        delete(TicketDraft).where(TicketDraft.id == draft_id).returning(TicketDraft.id),
    )
    return deleted_id is not None


async def set_prompt_message_id(
    session: AsyncSession,
    *,
    draft_id: uuid.UUID,
    expected_version: int,
    expected_step: DraftStep,
    message_id: int,
) -> bool:
    """Record the prompt message id WITHOUT bumping ``version``.

    This is a technical write (not a step transition): a late prompt-send must
    not overwrite the next step's prompt. Guarding on ``version`` and ``step``
    means the write is a no-op (returns ``False``) if the draft has since moved
    on to the next step or been changed.
    """
    updated_id = await session.scalar(
        update(TicketDraft)
        .where(
            TicketDraft.id == draft_id,
            TicketDraft.version == expected_version,
            TicketDraft.step == expected_step,
        )
        .values(prompt_message_id=message_id)
        .returning(TicketDraft.id),
    )
    return updated_id is not None


async def _step_update(
    session: AsyncSession,
    *,
    draft_id: uuid.UUID,
    expected_version: int,
    expected_step: DraftStep,
    values: dict[str, Any],
    next_step: DraftStep,
) -> TicketDraft:
    """Run the guarded step transition ``UPDATE ... RETURNING``.

    A stale version or a wrong step (answering the wrong question) matches zero
    rows and raises :class:`DraftChanged` without changing anything.
    """
    # NOTE: ``updated_at`` is intentionally NOT bumped here — this raw Core UPDATE
    # bypasses the ORM ``onupdate``; a cross-cutting pass will add
    # ``updated_at=func.now()`` to all business Core UPDATEs.
    result = await session.execute(
        update(TicketDraft)
        .where(
            TicketDraft.id == draft_id,
            TicketDraft.version == expected_version,
            TicketDraft.step == expected_step,
        )
        .values(version=TicketDraft.version + 1, step=next_step, prompt_message_id=None, **values)
        .returning(TicketDraft),
    )
    updated = result.scalar_one_or_none()
    if updated is None:
        exists = await session.scalar(select(TicketDraft.id).where(TicketDraft.id == draft_id))
        if exists is None:
            raise DraftNotFound(draft_id)
        raise DraftChanged(draft_id)
    return updated


async def save_description(
    session: AsyncSession, *, draft_id: uuid.UUID, expected_version: int, description: str
) -> TicketDraft:
    """Save the ticket description: ``description -> priority``."""
    stripped = description.strip()
    if not stripped:
        raise DraftValidationError("Описание не может быть пустым")
    if len(stripped) > 3000:
        raise DraftValidationError("Описание слишком длинное (максимум 3000 символов)")
    return await _step_update(
        session,
        draft_id=draft_id,
        expected_version=expected_version,
        expected_step=DraftStep.description,
        values={"description": stripped},
        next_step=DraftStep.priority,
    )


async def set_priority(
    session: AsyncSession, *, draft_id: uuid.UUID, expected_version: int, priority: Priority
) -> TicketDraft:
    """Save the ticket priority: ``priority -> request_id``."""
    return await _step_update(
        session,
        draft_id=draft_id,
        expected_version=expected_version,
        expected_step=DraftStep.priority,
        values={"priority": priority},
        next_step=DraftStep.request_id,
    )


async def save_request_id(
    session: AsyncSession, *, draft_id: uuid.UUID, expected_version: int, request_id: str
) -> TicketDraft:
    """Save the request id: ``request_id -> tenant_id``."""
    stripped = request_id.strip()
    if not stripped:
        raise DraftValidationError("ID заявки не может быть пустым")
    if len(stripped) > 64:
        raise DraftValidationError("ID заявки слишком длинный (максимум 64 символа)")
    return await _step_update(
        session,
        draft_id=draft_id,
        expected_version=expected_version,
        expected_step=DraftStep.request_id,
        values={"request_id": stripped},
        next_step=DraftStep.tenant_id,
    )


async def save_tenant_id(
    session: AsyncSession,
    *,
    draft_id: uuid.UUID,
    expected_version: int,
    tenant_id: str | None,
) -> TicketDraft:
    """Save the optional tenant id: ``tenant_id -> confirm`` (skippable)."""
    stripped: str | None
    if tenant_id is None or tenant_id.strip() == "":
        stripped = None
    else:
        stripped = tenant_id.strip()
        if len(stripped) > 64:
            raise DraftValidationError("ID арендатора слишком длинный (максимум 64 символа)")
    return await _step_update(
        session,
        draft_id=draft_id,
        expected_version=expected_version,
        expected_step=DraftStep.tenant_id,
        values={"tenant_id": stripped},
        next_step=DraftStep.confirm,
    )


async def confirm(
    session: AsyncSession, *, draft_id: uuid.UUID, expected_version: int, author: User
) -> Ticket:
    """Atomically claim the draft and create the ticket in one transaction.

    The ``DELETE ... RETURNING`` guarded by id+version is the atomic claim: two
    concurrent confirms cannot both create a ticket. If ``create_ticket`` raises,
    the delete rolls back with the caller's transaction.
    """
    draft = await session.get(TicketDraft, draft_id, populate_existing=True)
    if draft is None:
        raise DraftNotFound(draft_id)
    if draft.user_id != author.id:
        raise DraftNotFound(draft_id)  # never leak or act on another user's draft
    if draft.version != expected_version or draft.step is not DraftStep.confirm:
        raise DraftChanged(draft_id)
    if draft.description is None or draft.priority is None or draft.request_id is None:
        # Defensive: these are always set by the time the form reaches ``confirm``.
        raise DraftChanged(draft_id)

    claimed = await session.scalar(
        delete(TicketDraft)
        .where(TicketDraft.id == draft_id, TicketDraft.version == expected_version)
        .returning(TicketDraft.id),
    )
    if claimed is None:
        raise DraftChanged(draft_id)

    ticket = await create_ticket(
        session,
        author=author,
        priority=draft.priority,
        description=draft.description,
        request_id=draft.request_id,
        tenant_id=draft.tenant_id,
    )
    return ticket


async def cleanup_expired_drafts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    ttl_seconds: int | None = None,
) -> int:
    """Delete drafts older than ``ttl_seconds``; return the deleted count.

    ``created_at`` (draft age) is the "старше TTL" criterion, matching the
    spec's definition of an expired draft rather than last-update time.
    """
    if now is None:
        now = _now()
    if ttl_seconds is None:
        ttl_seconds = settings.draft_ttl_seconds
    cutoff = now - timedelta(seconds=ttl_seconds)
    result = await session.execute(
        delete(TicketDraft)
        .where(TicketDraft.created_at < cutoff)
        .returning(TicketDraft.id)  # strictly less-than
    )
    return len(result.scalars().all())
