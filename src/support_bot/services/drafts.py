import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.config import Settings
from support_bot.db.models import PRIORITIES, TicketDraft
from support_bot.domain.errors import DomainError, ValidationFailed
from support_bot.services import tickets as ticket_svc
from support_bot.services.validation import DESCRIPTION_MAX, ID_MAX, required_text

STEPS = ("description", "priority", "request_id", "tenant_id", "confirm")
TEXT_STEPS = frozenset({"description", "request_id", "tenant_id"})
_NEXT_STEP = dict(zip(STEPS, STEPS[1:], strict=False))

# Short callback codes; `d:<code>:<draft uuid hex>:<version>[:value]` stays under 64 bytes.
CALLBACK_CODES = {"pr": "priority", "skip": "skip", "ok": "confirm", "no": "cancel"}
_CODE_BY_ACTION = {action: code for code, action in CALLBACK_CODES.items()}

Kind = Literal["advanced", "reprompt", "invalid", "rejected", "created", "cancelled"]


@dataclass(frozen=True, slots=True)
class DraftState:
    draft_id: UUID
    version: int
    step: str
    values: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: Kind
    state: DraftState | None = None
    ticket_id: int | None = None
    priority: str | None = None
    message: str | None = None


def draft_callback(code: str, draft_id: UUID, version: int, value: str | None = None) -> str:
    base = f"d:{code}:{draft_id.hex}:{version}"
    return f"{base}:{value}" if value is not None else base


def parse_draft_callback(data: str) -> tuple[str, UUID, int, str | None] | None:
    parts = data.split(":")
    if len(parts) not in (4, 5) or parts[0] != "d" or parts[1] not in CALLBACK_CODES:
        return None
    try:
        draft_id = UUID(hex=parts[2])
        version = int(parts[3])
    except ValueError:
        return None
    value = parts[4] if len(parts) == 5 else None
    return parts[1], draft_id, version, value


def _state(row: TicketDraft) -> DraftState:
    return DraftState(
        draft_id=row.id,
        version=row.version,
        step=row.step,
        values={
            "description": row.description,
            "priority": row.priority,
            "request_id": row.request_id,
            "tenant_id": row.tenant_id,
        },
    )


def _is_expired(row: TicketDraft, now: datetime, ttl_hours: int) -> bool:
    return row.updated_at < now - timedelta(hours=ttl_hours)


async def _lock(
    session: AsyncSession, user_id: int, now: datetime, ttl_hours: int
) -> TicketDraft | None:
    row = await session.scalar(
        select(TicketDraft)
        .where(TicketDraft.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    if _is_expired(row, now, ttl_hours):
        await session.delete(row)
        await session.flush()
        return None
    return row


async def get_active(
    session: AsyncSession, user_id: int, *, now: datetime, ttl_hours: int
) -> DraftState | None:
    row = await session.scalar(
        select(TicketDraft)
        .where(TicketDraft.user_id == user_id)
        .execution_options(populate_existing=True)
    )
    if row is None or _is_expired(row, now, ttl_hours):
        return None
    return _state(row)


async def start(
    session: AsyncSession, user_id: int, *, now: datetime, ttl_hours: int
) -> DraftState:
    """Return the active draft, or create one. At most one draft per user."""
    existing = await get_active(session, user_id, now=now, ttl_hours=ttl_hours)
    if existing is not None:
        return existing
    await session.execute(delete(TicketDraft).where(TicketDraft.user_id == user_id))
    row = (
        await session.execute(
            insert(TicketDraft)
            .values(
                id=uuid.uuid4(),
                user_id=user_id,
                step=STEPS[0],
                version=1,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[TicketDraft.user_id])
            .returning(TicketDraft.id)
        )
    ).scalar_one_or_none()
    if row is None:
        active = await get_active(session, user_id, now=now, ttl_hours=ttl_hours)
        assert active is not None
        return active
    created = await session.get(TicketDraft, row, populate_existing=True)
    assert created is not None
    return _state(created)


async def restart(session: AsyncSession, user_id: int, *, now: datetime) -> DraftState:
    await session.execute(delete(TicketDraft).where(TicketDraft.user_id == user_id))
    row = TicketDraft(
        id=uuid.uuid4(),
        user_id=user_id,
        step=STEPS[0],
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return _state(row)


async def cancel(session: AsyncSession, user_id: int) -> bool:
    result = await session.execute(delete(TicketDraft).where(TicketDraft.user_id == user_id))
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def purge_expired(session: AsyncSession, *, now: datetime, ttl_hours: int) -> int:
    result = await session.execute(
        delete(TicketDraft).where(TicketDraft.updated_at < now - timedelta(hours=ttl_hours))
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


async def record_prompt(
    session: AsyncSession,
    *,
    draft_id: UUID,
    version: int,
    step: str,
    message_id: int,
    now: datetime,
) -> bool:
    """Store the message id of a question. Technical write: does not bump version.

    Applies only if the draft is still at the version and step the question was built
    for, so a late send cannot overwrite the prompt of a newer step.
    """
    result = await session.execute(
        update(TicketDraft)
        .where(
            TicketDraft.id == draft_id,
            TicketDraft.version == version,
            TicketDraft.step == step,
        )
        .values(prompt_message_id=message_id, updated_at=now)
        .returning(TicketDraft.id)
    )
    return result.scalar_one_or_none() is not None


def _validate(step: str, text: str) -> str:
    if step == "description":
        return required_text(text, field="Описание", max_length=DESCRIPTION_MAX)
    if step == "request_id":
        return required_text(text, field="ID заявки", max_length=ID_MAX)
    if step == "tenant_id":
        return required_text(text, field="ID арендатора", max_length=ID_MAX)
    raise AssertionError(f"not a text step: {step}")


def _advance(row: TicketDraft, now: datetime) -> None:
    row.step = _NEXT_STEP[row.step]
    row.version += 1
    row.prompt_message_id = None
    row.updated_at = now


async def answer_text(
    session: AsyncSession,
    *,
    user_id: int,
    reply_to_message_id: int | None,
    text: str,
    now: datetime,
    ttl_hours: int,
) -> Outcome:
    row = await _lock(session, user_id, now, ttl_hours)
    if row is None:
        return Outcome("rejected", message="Черновик не найден. Начните с /new")
    state = _state(row)
    if row.step not in TEXT_STEPS:
        return Outcome("reprompt", state)
    if row.prompt_message_id is None or reply_to_message_id != row.prompt_message_id:
        return Outcome("reprompt", state)
    try:
        value = _validate(row.step, text)
    except ValidationFailed as exc:
        return Outcome("invalid", state, message=str(exc))
    _store(row, value)
    _advance(row, now)
    return Outcome("advanced", _state(row))


def _store(row: TicketDraft, value: str | None) -> None:
    if row.step == "description":
        row.description = value
    elif row.step == "request_id":
        row.request_id = value
    elif row.step == "tenant_id":
        row.tenant_id = value
    elif row.step == "priority":
        row.priority = value


async def press(
    session: AsyncSession,
    *,
    user_id: int,
    draft_id: UUID,
    version: int,
    action: str,
    value: str | None,
    now: datetime,
    ttl_hours: int,
    settings: Settings,
) -> Outcome:
    row = await _lock(session, user_id, now, ttl_hours)
    if row is None or row.id != draft_id or row.version != version:
        current = await _current(session, user_id, now, ttl_hours)
        return Outcome(
            "rejected",
            current,
            message="Эта кнопка устарела. Продолжите с актуального шага",
        )

    state = _state(row)
    if action == "cancel":
        await session.delete(row)
        await session.flush()
        return Outcome("cancelled")
    if action == "priority" and row.step == "priority" and value in PRIORITIES:
        _store(row, value)
        _advance(row, now)
        return Outcome("advanced", _state(row))
    if action == "skip" and row.step == "tenant_id":
        _store(row, None)
        _advance(row, now)
        return Outcome("advanced", _state(row))
    if action == "confirm" and row.step == "confirm":
        return await _create(session, user_id, row, settings, now)
    return Outcome("rejected", state, message="Этот шаг сейчас недоступен")


async def _create(
    session: AsyncSession,
    user_id: int,
    row: TicketDraft,
    settings: Settings,
    now: datetime,
) -> Outcome:
    if row.description is None or row.priority is None or row.request_id is None:
        raise DomainError("Черновик заполнен не полностью")
    ticket = await ticket_svc.create_ticket(
        session,
        settings=settings,
        actor_id=user_id,
        description=row.description,
        priority=row.priority,
        request_id=row.request_id,
        tenant_id=row.tenant_id,
        now=now,
    )
    await session.delete(row)
    await session.flush()
    return Outcome("created", ticket_id=ticket.id, priority=ticket.priority)


async def _current(
    session: AsyncSession, user_id: int, now: datetime, ttl_hours: int
) -> DraftState | None:
    return await get_active(session, user_id, now=now, ttl_hours=ttl_hours)
