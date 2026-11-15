"""Draft (анкета) persistence with optimistic versioning.

Version rules (spec): rejected/stale updates (foreign draft id, stale version, reply
without the required binding) must not change data, step or version. Every accepted
answer bumps version exactly once inside the caller's transaction. The prompt message
id is saved only with a conditional UPDATE keyed on the (version, step) the question
was composed for, so a late send cannot clobber a newer question's binding.
"""

from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import DraftStep, TicketDraft

TEXT_STEPS = (DraftStep.description, DraftStep.request_id, DraftStep.tenant_id)

NEXT_STEP: dict[DraftStep, DraftStep] = {
    DraftStep.description: DraftStep.priority,
    DraftStep.priority: DraftStep.request_id,
    DraftStep.request_id: DraftStep.tenant_id,
    DraftStep.tenant_id: DraftStep.confirm,
}


async def get_for_user(session: AsyncSession, user_id: int) -> TicketDraft | None:
    return await session.scalar(select(TicketDraft).where(TicketDraft.user_id == user_id))


async def get_for_user_locked(session: AsyncSession, user_id: int) -> TicketDraft | None:
    return await session.scalar(
        select(TicketDraft).where(TicketDraft.user_id == user_id).with_for_update()
    )


async def lock(session: AsyncSession, draft_id: UUID) -> TicketDraft | None:
    return await session.scalar(
        select(TicketDraft).where(TicketDraft.id == draft_id).with_for_update()
    )


async def create_draft(session: AsyncSession, user_id: int) -> TicketDraft:
    draft = TicketDraft(user_id=user_id, step=DraftStep.description, version=1)
    session.add(draft)
    await session.flush()
    return draft


async def delete_draft(session: AsyncSession, draft_id: UUID) -> None:
    await session.execute(delete(TicketDraft).where(TicketDraft.id == draft_id))


async def save_answer(
    session: AsyncSession,
    draft_id: UUID,
    *,
    expected_version: int,
    expected_step: DraftStep,
    field_values: dict[str, Any],
    new_step: DraftStep,
) -> bool:
    """Conditional answer save: bumps version only if (version, step) still match."""
    values = {**field_values, "step": new_step, "version": expected_version + 1}
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(TicketDraft)
            .where(
                TicketDraft.id == draft_id,
                TicketDraft.version == expected_version,
                TicketDraft.step == expected_step,
            )
            .values(**values),
            execution_options={"synchronize_session": False},
        ),
    )
    return result.rowcount > 0


async def set_prompt(
    session_factory: async_sessionmaker[AsyncSession],
    draft_id: UUID,
    *,
    version: int,
    step: DraftStep,
    message_id: int,
) -> bool:
    """Own short tx (no tx held across the Telegram call, spec rule).

    Overwrites prompt_message_id only for the (version, step) the question was
    composed for; if the draft moved on, the binding is left to the newer question.
    """
    async with session_factory() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                update(TicketDraft)
                .where(
                    TicketDraft.id == draft_id,
                    TicketDraft.version == version,
                    TicketDraft.step == step,
                )
                .values(prompt_message_id=message_id),
                execution_options={"synchronize_session": False},
            ),
        )
    return result.rowcount > 0


async def cleanup_expired(
    session_factory: async_sessionmaker[AsyncSession], ttl_hours: float
) -> int:
    async with session_factory() as session, session.begin():
        result = cast(
            CursorResult[Any],
            await session.execute(
                delete(TicketDraft).where(
                    TicketDraft.created_at
                    < text("now() - make_interval(hours => :hours)").bindparams(
                        hours=int(ttl_hours)
                    )
                )
            ),
        )
    return result.rowcount
