"""Черновик формы обращения (SPEC «Создание обращения»).

Много-репликовая безопасность:
- один активный черновик на пользователя (UNIQUE user_id);
- изменение данных/шага — в транзакции под SELECT ... FOR UPDATE, version + 1 ровно один раз;
- отклонённые/устаревшие апдейты ничего не меняют (данные, шаг, version);
- prompt_message_id — technical-запись, version не увеличивает, сохраняется
  условным UPDATE (draft_id + version + step).
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import Draft, Ticket, TicketEvent, User
from app.services.errors import DraftValidationError, StaleDraftError, StaleAnswerError


@dataclass(frozen=True)
class DraftResult:
    step: str | None  # новый шаг (None — черновик удалён)
    version: int | None
    ticket_id: int | None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_draft(session: AsyncSession, user_id: int) -> Draft | None:
    return (
        await session.execute(select(Draft).where(Draft.user_id == user_id))
    ).scalar_one_or_none()


async def start_draft(
    session: AsyncSession, user_id: int, *, reset: bool = False
) -> tuple[Draft, bool]:
    """(draft, created). При reset=True существующий черновик заменяется новым."""
    draft = await get_draft(session, user_id)
    if draft is not None:
        if not reset:
            return draft, False
        await session.execute(delete(Draft).where(Draft.id == draft.id))
        await session.flush()
    draft = Draft(user_id=user_id, step="description", version=1)
    session.add(draft)
    await session.flush()
    return draft, True


async def reset_draft(
    session: AsyncSession, user_id: int, draft_id: _uuid.UUID, expected_version: int
) -> Draft:
    """«Начать заново»: проверка id+version, удаление, новый черновик."""
    draft = (
        await session.execute(
            select(Draft).where(Draft.id == draft_id, Draft.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if draft is None or draft.version != expected_version:
        raise StaleDraftError()
    await session.execute(delete(Draft).where(Draft.id == draft.id))
    await session.flush()
    new = Draft(user_id=user_id, step="description", version=1)
    session.add(new)
    await session.flush()
    return new


async def answer_text(
    session: AsyncSession, user_id: int, raw_text: str, reply_to_message_id: int | None
) -> DraftResult:
    """Текстовый ответ, привязанный к prompt_message_id. Устаревший ответ ничего не меняет."""
    draft = (
        await session.execute(select(Draft).where(Draft.user_id == user_id).with_for_update())
    ).scalar_one_or_none()
    if draft is None:
        raise StaleAnswerError()
    if draft.prompt_message_id is None or reply_to_message_id != draft.prompt_message_id:
        raise StaleAnswerError()
    text_value = raw_text.strip()
    if draft.step == "description":
        if not text_value or len(text_value) > 3000:
            raise DraftValidationError("описания")
        draft.description = text_value
        draft.step = "priority"
    elif draft.step == "request_id":
        if not text_value or len(text_value) > 64:
            raise DraftValidationError("id заявки")
        draft.request_id = text_value
        draft.step = "tenant_id"
    elif draft.step == "tenant_id":
        if not text_value or len(text_value) > 64:
            raise DraftValidationError("id арендатора")
        draft.tenant_id = text_value
        draft.step = "confirm"
    else:
        raise StaleAnswerError()
    draft.prompt_message_id = None
    draft.version += 1
    await session.flush()
    return DraftResult(step=draft.step, version=draft.version, ticket_id=None)


async def answer_choice(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    draft_id: _uuid.UUID,
    expected_version: int,
    choice: str,
    value: str | None = None,
) -> DraftResult:
    """Кнопка формы: prio / skip / send / canx. Чужой id или устаревшая version → StaleDraftError."""
    draft = (
        await session.execute(
            select(Draft).where(Draft.id == draft_id, Draft.user_id == user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if draft is None or draft.version != expected_version:
        raise StaleDraftError()

    if choice == "prio":
        if draft.step != "priority" or value not in ("critical", "high", "medium", "low"):
            raise StaleDraftError()
        draft.priority = value
        draft.step = "request_id"
    elif choice == "skip":
        if draft.step != "tenant_id":
            raise StaleDraftError()
        draft.tenant_id = None
        draft.step = "confirm"
    elif choice == "canx":
        await session.execute(delete(Draft).where(Draft.id == draft.id))
        await session.flush()
        return DraftResult(step=None, version=None, ticket_id=None)
    elif choice == "send":
        # «Отправить» в одной транзакции: создание обращения + удаление черновика.
        if draft.step != "confirm":
            raise StaleDraftError()
        if not draft.description or not draft.priority or not draft.request_id:
            raise StaleDraftError()
        ticket = Ticket(
            author_id=user_id,
            description=draft.description,
            priority=draft.priority,
            request_id=draft.request_id,
            tenant_id=draft.tenant_id,
            status="new",
            version=1,
            next_reminder_at=_now() + timedelta(seconds=settings.reminder_interval(draft.priority)),
        )
        session.add(ticket)
        await session.flush()
        session.add(
            TicketEvent(
                ticket_id=ticket.id,
                event_type="created",
                actor_id=user_id,
                new_status="new",
                version=1,
            )
        )
        from app.metrics import tickets_created_total

        tickets_created_total.labels(priority=draft.priority).inc()
        await session.execute(delete(Draft).where(Draft.id == draft.id))
        await session.flush()
        return DraftResult(step=None, version=None, ticket_id=ticket.id)
    else:
        raise StaleDraftError()

    draft.prompt_message_id = None
    draft.version += 1
    await session.flush()
    return DraftResult(step=draft.step, version=draft.version, ticket_id=None)


async def save_prompt_message_id(
    session_factory: async_sessionmaker[AsyncSession],
    draft_id: _uuid.UUID,
    version: int,
    step: str,
    message_id: int,
) -> None:
    """Technical-запись (version не увеличивается). Условное обновление:
    draft_id+version+step — те, для которых вопрос был сформирован.
    Если черновик изменился/удалён — prompt_message_id не перезаписывается.
    """
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Draft)
            .where(Draft.id == draft_id, Draft.version == version, Draft.step == step)
            .values(prompt_message_id=message_id)
        )


async def delete_draft(
    session: AsyncSession,
    user_id: int,
    draft_id: _uuid.UUID | None = None,
    expected_version: int | None = None,
) -> bool:
    q = delete(Draft).where(Draft.user_id == user_id)
    if draft_id is not None:
        q = q.where(Draft.id == draft_id)
    if expected_version is not None:
        q = q.where(Draft.version == expected_version)
    result = cast(CursorResult[()], await session.execute(q))
    await session.flush()
    return (result.rowcount or 0) > 0


async def delete_expired_drafts(
    session_factory: async_sessionmaker[AsyncSession], ttl_hours: int
) -> int:
    cutoff = _now() - timedelta(hours=ttl_hours)
    async with session_factory() as session, session.begin():
        result = cast(
            CursorResult[()],
            await session.execute(delete(Draft).where(Draft.updated_at < cutoff)),
        )
        return result.rowcount or 0
