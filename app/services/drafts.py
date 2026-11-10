from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TicketDraft, User
from app.services.tickets import Conflict, Forbidden, create_ticket


async def draft_for_user(
    session: AsyncSession, user_id: int, *, lock: bool = False
) -> TicketDraft | None:
    stmt = select(TicketDraft).where(TicketDraft.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    return await session.scalar(stmt)


async def new_draft(session: AsyncSession, user_id: int, restart: bool = False) -> TicketDraft:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None or user.role not in {"support", "admin"}:
        raise Forbidden()
    existing = await draft_for_user(session, user_id, lock=True)
    if existing and not restart:
        return existing
    if existing:
        await session.delete(existing)
        await session.flush()
    draft = TicketDraft(user_id=user_id, step="description", version=1)
    session.add(draft)
    await session.flush()
    return draft


async def cancel_draft(session: AsyncSession, user_id: int) -> None:
    await session.execute(delete(TicketDraft).where(TicketDraft.user_id == user_id))


async def save_prompt(
    session: AsyncSession, draft_id: UUID, version: int, step: str, message_id: int
) -> None:
    await session.execute(
        update(TicketDraft)
        .where(TicketDraft.id == draft_id, TicketDraft.version == version, TicketDraft.step == step)
        .values(prompt_message_id=message_id)
    )


async def advance_text(
    session: AsyncSession, user_id: int, reply_id: int | None, value: str
) -> TicketDraft:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update(read=True))
    if user is None or user.role not in {"support", "admin"}:
        raise Forbidden()
    draft = await draft_for_user(session, user_id, lock=True)
    if draft is None:
        raise LookupError("No draft")
    if not draft.prompt_message_id or draft.prompt_message_id != reply_id:
        raise Conflict()
    value = value.strip()
    if draft.step == "description" and 0 < len(value) <= 3000:
        draft.description, draft.step = value, "priority"
    elif draft.step == "request_id" and 0 < len(value) <= 64:
        draft.request_id, draft.step = value, "tenant_id"
    elif draft.step == "tenant_id" and 0 < len(value) <= 64:
        draft.tenant_id, draft.step = value, "confirm"
    else:
        raise ValueError("Invalid value or step")
    draft.prompt_message_id = None
    draft.version += 1
    await session.flush()
    return draft


async def advance_button(
    session: AsyncSession, user_id: int, draft_id: UUID, version: int, action: str, intervals=None
):
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update(read=True))
    if user is None or user.role not in {"support", "admin"}:
        raise Forbidden()
    draft = await draft_for_user(session, user_id, lock=True)
    if draft is None or draft.id != draft_id or draft.version != version:
        raise Conflict()
    if draft.step == "priority" and action in {"critical", "high", "medium", "low"}:
        draft.priority, draft.step = action, "request_id"
    elif draft.step == "tenant_id" and action == "skip":
        draft.tenant_id, draft.step = None, "confirm"
    elif draft.step == "confirm" and action == "submit":
        if draft.description is None or draft.priority is None or draft.request_id is None:
            raise Conflict()
        kwargs = {"intervals": intervals} if intervals is not None else {}
        ticket = await create_ticket(
            session,
            user_id,
            draft.description,
            draft.priority,
            draft.request_id,
            draft.tenant_id,
            **kwargs,
        )
        await session.delete(draft)
        return ticket
    else:
        raise Conflict()
    draft.version += 1
    draft.prompt_message_id = None
    await session.flush()
    return draft
