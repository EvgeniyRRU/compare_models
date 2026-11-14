from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.db.enums import DraftStep, TicketPriority, UserRole
from support_bot.db.models.ticket import Ticket
from support_bot.db.models.ticket_draft import TicketDraft
from support_bot.domain.errors import (
    DraftConflict,
    DraftValidationError,
    NoActiveDraft,
    PermissionDenied,
)
from support_bot.domain.reminders import ReminderPolicy
from support_bot.services.tickets import (
    MAX_DESCRIPTION_LENGTH,
    MAX_ID_LENGTH,
    TicketInput,
    _insert_ticket,
    utcnow,
)

TEXT_STEPS: frozenset[DraftStep] = frozenset(
    {DraftStep.DESCRIPTION, DraftStep.REQUEST_ID, DraftStep.TENANT_ID}
)
NEXT_STEP: dict[DraftStep, DraftStep] = {
    DraftStep.DESCRIPTION: DraftStep.PRIORITY,
    DraftStep.PRIORITY: DraftStep.REQUEST_ID,
    DraftStep.REQUEST_ID: DraftStep.TENANT_ID,
    DraftStep.TENANT_ID: DraftStep.CONFIRM,
}


@dataclass(frozen=True, slots=True)
class DraftSnapshot:
    id: uuid.UUID
    user_id: int
    version: int
    step: DraftStep
    description: str | None
    priority: TicketPriority | None
    request_id: str | None
    tenant_id: str | None
    prompt_message_id: int | None


def _snapshot(draft: TicketDraft) -> DraftSnapshot:
    return DraftSnapshot(
        id=draft.id,
        user_id=draft.user_id,
        version=draft.version,
        step=draft.step,
        description=draft.description,
        priority=draft.priority,
        request_id=draft.request_id,
        tenant_id=draft.tenant_id,
        prompt_message_id=draft.prompt_message_id,
    )


def _validate_text(step: DraftStep, text: str) -> str:
    text = text.strip()
    if not text:
        raise DraftValidationError("Поле не может быть пустым. Введите значение ещё раз.")
    limit = MAX_DESCRIPTION_LENGTH if step is DraftStep.DESCRIPTION else MAX_ID_LENGTH
    if len(text) > limit:
        raise DraftValidationError(f"Слишком длинное значение: максимум {limit} символов.")
    return text


async def existing_active(
    session: AsyncSession, user_id: int, *, ttl_hours: int
) -> DraftSnapshot | None:
    async with session.begin():
        draft = (
            await session.execute(
                select(TicketDraft).where(TicketDraft.user_id == user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if draft is None:
            return None
        if draft.updated_at < utcnow() - timedelta(hours=ttl_hours):
            await session.delete(draft)
            return None
        return _snapshot(draft)


async def create_or_get(session: AsyncSession, user_id: int) -> tuple[DraftSnapshot, bool]:
    """Create a fresh draft if none exists. Returns (snapshot, created)."""
    new_id = uuid.uuid4()
    async with session.begin():
        stmt = (
            pg_insert(TicketDraft)
            .values(
                id=new_id,
                user_id=user_id,
                step=DraftStep.DESCRIPTION,
                version=1,
            )
            .on_conflict_do_nothing(index_elements=[TicketDraft.user_id])
            .returning(TicketDraft.id)
        )
        inserted_id = await session.scalar(stmt)
        if inserted_id is None:
            existing = (
                await session.execute(select(TicketDraft).where(TicketDraft.user_id == user_id))
            ).scalar_one()
            return _snapshot(existing), False
        draft = await session.get(TicketDraft, inserted_id)
        if draft is None:
            raise DraftConflict("draft disappeared")
        return _snapshot(draft), True


async def restart(
    session: AsyncSession,
    *,
    user_id: int,
    expected_draft_id: uuid.UUID,
    expected_version: int,
) -> DraftSnapshot:
    new_id = uuid.uuid4()
    async with session.begin():
        deleted_id = await session.scalar(
            sa_delete(TicketDraft)
            .where(
                TicketDraft.id == expected_draft_id,
                TicketDraft.user_id == user_id,
                TicketDraft.version == expected_version,
            )
            .returning(TicketDraft.id)
        )
        if deleted_id is None:
            raise DraftConflict("draft changed")
        await session.flush()
        session.add(
            TicketDraft(
                id=new_id,
                user_id=user_id,
                step=DraftStep.DESCRIPTION,
                version=1,
            )
        )
        await session.flush()
        draft = await session.get(TicketDraft, new_id)
        if draft is None:
            raise DraftConflict("draft disappeared")
        return _snapshot(draft)


async def delete_draft(
    session: AsyncSession,
    *,
    user_id: int,
    draft_id: uuid.UUID,
    version: int,
) -> bool:
    async with session.begin():
        deleted_id = await session.scalar(
            sa_delete(TicketDraft)
            .where(
                TicketDraft.id == draft_id,
                TicketDraft.user_id == user_id,
                TicketDraft.version == version,
            )
            .returning(TicketDraft.id)
        )
        return deleted_id is not None


async def delete_active(session: AsyncSession, user_id: int) -> bool:
    async with session.begin():
        deleted_id = await session.scalar(
            sa_delete(TicketDraft).where(TicketDraft.user_id == user_id).returning(TicketDraft.id)
        )
        return deleted_id is not None


async def get(session: AsyncSession, draft_id: uuid.UUID) -> DraftSnapshot | None:
    draft = await session.get(TicketDraft, draft_id)
    return _snapshot(draft) if draft is not None else None


async def submit_text(
    session: AsyncSession,
    *,
    user_id: int,
    text: str,
    reply_to_message_id: int | None,
) -> DraftSnapshot:
    async with session.begin():
        draft = (
            await session.execute(
                select(TicketDraft).where(TicketDraft.user_id == user_id).with_for_update()
            )
        ).scalar_one_or_none()
        if draft is None:
            raise NoActiveDraft()
        if draft.step not in TEXT_STEPS:
            raise DraftConflict("draft is not waiting for text")
        if (
            draft.prompt_message_id is None
            or reply_to_message_id is None
            or reply_to_message_id != draft.prompt_message_id
        ):
            raise DraftConflict("message is not a reply to the current question")

        value = _validate_text(draft.step, text)
        if draft.step is DraftStep.DESCRIPTION:
            draft.description = value
        elif draft.step is DraftStep.REQUEST_ID:
            draft.request_id = value
        else:
            draft.tenant_id = value

        draft.step = NEXT_STEP[draft.step]
        draft.prompt_message_id = None
        draft.version += 1
        await session.flush()
        return _snapshot(draft)


async def choose_priority(
    session: AsyncSession,
    *,
    user_id: int,
    draft_id: uuid.UUID,
    version: int,
    priority: TicketPriority,
) -> DraftSnapshot:
    async with session.begin():
        draft = await _locked_draft(session, draft_id)
        _ensure_current(draft, user_id, version, DraftStep.PRIORITY)
        draft.priority = priority
        draft.step = DraftStep.REQUEST_ID
        draft.prompt_message_id = None
        draft.version += 1
        await session.flush()
        return _snapshot(draft)


async def skip_tenant(
    session: AsyncSession,
    *,
    user_id: int,
    draft_id: uuid.UUID,
    version: int,
) -> DraftSnapshot:
    async with session.begin():
        draft = await _locked_draft(session, draft_id)
        _ensure_current(draft, user_id, version, DraftStep.TENANT_ID)
        draft.tenant_id = None
        draft.step = DraftStep.CONFIRM
        draft.prompt_message_id = None
        draft.version += 1
        await session.flush()
        return _snapshot(draft)


async def confirm(
    session: AsyncSession,
    *,
    user_id: int,
    draft_id: uuid.UUID,
    version: int,
    policy: ReminderPolicy,
) -> Ticket:
    async with session.begin():
        draft = await _locked_draft(session, draft_id)
        _ensure_current(draft, user_id, version, DraftStep.CONFIRM)
        if not draft.description or not draft.priority or not draft.request_id:
            raise DraftConflict("draft is incomplete")

        from support_bot.services import users as users_service

        role = await users_service.get_role(session, user_id)
        if role not in (UserRole.SUPPORT, UserRole.ADMIN):
            raise PermissionDenied("support or admin role required")

        ticket = await _insert_ticket(
            session,
            actor_id=user_id,
            data=TicketInput(
                description=draft.description,
                priority=draft.priority,
                request_id=draft.request_id,
                tenant_id=draft.tenant_id,
            ),
            policy=policy,
            now=utcnow(),
        )
        await session.delete(draft)
    return ticket


async def set_prompt_message_id(
    session: AsyncSession,
    *,
    draft_id: uuid.UUID,
    version: int,
    step: DraftStep,
    message_id: int,
) -> bool:
    """Technical write: only applies if the draft is still at the rendered version/step."""
    async with session.begin():
        updated_id = await session.scalar(
            update(TicketDraft)
            .where(
                TicketDraft.id == draft_id,
                TicketDraft.version == version,
                TicketDraft.step == step,
            )
            .values(prompt_message_id=message_id)
            .returning(TicketDraft.id)
        )
        return updated_id is not None


async def _locked_draft(session: AsyncSession, draft_id: uuid.UUID) -> TicketDraft:
    draft = (
        await session.execute(
            select(TicketDraft).where(TicketDraft.id == draft_id).with_for_update()
        )
    ).scalar_one_or_none()
    if draft is None:
        raise DraftConflict("draft deleted")
    return draft


def _ensure_current(draft: TicketDraft, user_id: int, version: int, step: DraftStep) -> None:
    if draft.user_id != user_id or draft.version != version or draft.step != step:
        raise DraftConflict("draft changed")


__all__ = [
    "NEXT_STEP",
    "TEXT_STEPS",
    "DraftSnapshot",
    "choose_priority",
    "confirm",
    "create_or_get",
    "delete_active",
    "delete_draft",
    "existing_active",
    "get",
    "restart",
    "set_prompt_message_id",
    "skip_tenant",
    "submit_text",
]
