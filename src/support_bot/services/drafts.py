"""Пошаговая форма создания обращения на таблице ticket_drafts.

Состояние живёт только в БД, поэтому соседние апдейты одного пользователя могут
обрабатываться разными репликами. Правила:

* успешное изменение данных/шага — под SELECT ... FOR UPDATE, version += 1 ровно один раз;
* отклонённые и устаревшие апдейты ничего не меняют;
* prompt_message_id — техническая запись, сохраняется условным UPDATE без изменения version.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import Interval, Select, delete, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.db.engine import SessionFactory
from support_bot.db.models import TicketDraft
from support_bot.domain.enums import TEXT_STEPS, DraftStep, Priority
from support_bot.domain.errors import DomainError, PermissionDeniedError, ValidationFailedError
from support_bot.domain.state_machine import can_create
from support_bot.domain.validation import (
    TicketData,
    clean_description,
    clean_request_id,
    clean_tenant_id,
)
from support_bot.monitoring import metrics
from support_bot.services.tickets import TicketService
from support_bot.services.users import load_actor


@dataclass(frozen=True)
class DraftSnapshot:
    id: uuid.UUID
    user_id: int
    step: DraftStep
    version: int
    description: str | None
    priority: Priority | None
    request_id: str | None
    tenant_id: str | None
    prompt_message_id: int | None

    @classmethod
    def from_model(cls, d: TicketDraft) -> DraftSnapshot:
        return cls(
            d.id,
            d.user_id,
            d.step,
            d.version,
            d.description,
            d.priority,
            d.request_id,
            d.tenant_id,
            d.prompt_message_id,
        )


class DraftNotFoundError(DomainError):
    default_message = "Черновик не найден. Чтобы создать обращение, отправьте /new"


class DraftStaleError(DomainError):
    default_message = "Эта кнопка устарела. Продолжите с текущего шага формы."

    def __init__(self, current: DraftSnapshot | None) -> None:
        self.current = current
        super().__init__()


class TextOutcome(StrEnum):
    ACCEPTED = "accepted"  # ответ сохранён, черновик на следующем шаге
    UNBOUND = "unbound"  # не ответ на текущий вопрос — задать вопрос заново
    INVALID = "invalid"  # ответ не прошёл валидацию — задать вопрос заново с ошибкой


@dataclass(frozen=True)
class TextResult:
    outcome: TextOutcome
    draft: DraftSnapshot
    error: str | None = None


@dataclass(frozen=True)
class StartResult:
    draft: DraftSnapshot
    existing: bool


_NEXT_STEP: dict[DraftStep, DraftStep] = {
    DraftStep.DESCRIPTION: DraftStep.PRIORITY,
    DraftStep.PRIORITY: DraftStep.REQUEST_ID,
    DraftStep.REQUEST_ID: DraftStep.TENANT_ID,
    DraftStep.TENANT_ID: DraftStep.CONFIRM,
}


class DraftService:
    def __init__(
        self, session_factory: SessionFactory, tickets: TicketService, ttl: timedelta
    ) -> None:
        self._sf = session_factory
        self._tickets = tickets
        self._ttl = ttl

    # --- запуск / отмена -------------------------------------------------------------------

    async def start(self, user_id: int) -> StartResult:
        """/new: вернуть активный черновик (existing=True) или создать новый."""
        async with self._sf.begin() as session:
            await self._require_creator(session, user_id)
            draft = await self._lock_active(session, user_id)
            if draft is not None:
                return StartResult(DraftSnapshot.from_model(draft), existing=True)
            return StartResult(await self._create(session, user_id), existing=False)

    async def restart(self, user_id: int, draft_id: uuid.UUID, version: int) -> DraftSnapshot:
        async with self._sf.begin() as session:
            await self._require_creator(session, user_id)
            await self._lock_for_callback(session, user_id, draft_id, version)
            return await self._create(session, user_id)

    async def resume(self, user_id: int, draft_id: uuid.UUID, version: int) -> DraftSnapshot:
        """«Продолжить»: проверить кнопку и вернуть текущий шаг (данные не меняются)."""
        async with self._sf.begin() as session:
            await self._require_creator(session, user_id)
            draft = await self._lock_for_callback(session, user_id, draft_id, version)
            return DraftSnapshot.from_model(draft)

    async def current(self, user_id: int) -> DraftSnapshot | None:
        async with self._sf() as session:
            draft = await self._get_active(session, user_id)
            return DraftSnapshot.from_model(draft) if draft else None

    async def cancel(self, user_id: int) -> bool:
        """/cancel в любой момент: удалить черновик пользователя."""
        async with self._sf.begin() as session:
            result = await session.execute(
                delete(TicketDraft).where(TicketDraft.user_id == user_id).returning(TicketDraft.id)
            )
            return result.first() is not None

    async def cancel_by_button(self, user_id: int, draft_id: uuid.UUID, version: int) -> None:
        async with self._sf.begin() as session:
            draft = await self._lock_for_callback(session, user_id, draft_id, version)
            await session.delete(draft)

    # --- шаги формы ------------------------------------------------------------------------

    async def submit_text(
        self, user_id: int, reply_to_message_id: int | None, text: str | None
    ) -> TextResult:
        async with self._sf.begin() as session:
            await self._require_creator(session, user_id)
            draft = await self._lock_active(session, user_id)
            if draft is None:
                raise DraftNotFoundError()
            snapshot = DraftSnapshot.from_model(draft)
            bound = (
                draft.step in TEXT_STEPS
                and draft.prompt_message_id is not None
                and reply_to_message_id == draft.prompt_message_id
            )
            if not bound:
                return TextResult(TextOutcome.UNBOUND, snapshot)
            try:
                match draft.step:
                    case DraftStep.DESCRIPTION:
                        draft.description = clean_description(text)
                    case DraftStep.REQUEST_ID:
                        draft.request_id = clean_request_id(text)
                    case DraftStep.TENANT_ID:
                        draft.tenant_id = clean_tenant_id(text)
                    case _:  # pragma: no cover - исключено проверкой TEXT_STEPS
                        return TextResult(TextOutcome.UNBOUND, snapshot)
            except ValidationFailedError as exc:
                # Значение вычисляется до присваивания, поэтому черновик не изменён.
                return TextResult(TextOutcome.INVALID, snapshot, exc.message)
            self._advance(draft)
            await session.flush()
            return TextResult(TextOutcome.ACCEPTED, DraftSnapshot.from_model(draft))

    async def choose_priority(
        self, user_id: int, draft_id: uuid.UUID, version: int, priority: Priority
    ) -> DraftSnapshot:
        async with self._sf.begin() as session:
            await self._require_creator(session, user_id)
            draft = await self._lock_for_callback(
                session, user_id, draft_id, version, DraftStep.PRIORITY
            )
            draft.priority = priority
            self._advance(draft)
            await session.flush()
            return DraftSnapshot.from_model(draft)

    async def skip_tenant(self, user_id: int, draft_id: uuid.UUID, version: int) -> DraftSnapshot:
        async with self._sf.begin() as session:
            await self._require_creator(session, user_id)
            draft = await self._lock_for_callback(
                session, user_id, draft_id, version, DraftStep.TENANT_ID
            )
            draft.tenant_id = None
            self._advance(draft)
            await session.flush()
            return DraftSnapshot.from_model(draft)

    async def submit(self, user_id: int, draft_id: uuid.UUID, version: int) -> int:
        """«Отправить»: создать обращение и удалить черновик одной транзакцией."""
        async with self._sf.begin() as session:
            draft = await self._lock_for_callback(
                session, user_id, draft_id, version, DraftStep.CONFIRM
            )
            data = TicketData.validated(
                draft.description, draft.priority, draft.request_id, draft.tenant_id
            )
            ticket_id = await self._tickets.insert_ticket(session, user_id, data)
            await session.delete(draft)
        metrics.TICKETS_CREATED.labels(priority=data.priority.value).inc()
        return ticket_id

    # --- техническая запись ----------------------------------------------------------------

    async def save_prompt(
        self, draft_id: uuid.UUID, version: int, step: DraftStep, message_id: int
    ) -> bool:
        """Сохранить message_id отправленного вопроса, если черновик не изменился."""
        async with self._sf.begin() as session:
            result = await session.execute(
                update(TicketDraft)
                .where(
                    TicketDraft.id == draft_id,
                    TicketDraft.version == version,
                    TicketDraft.step == step,
                )
                .values(prompt_message_id=message_id)
                .returning(TicketDraft.id)
            )
            return result.first() is not None

    async def delete_expired(self) -> int:
        async with self._sf.begin() as session:
            result = await session.execute(
                delete(TicketDraft)
                .where(TicketDraft.updated_at < func.now() - literal(self._ttl, Interval()))
                .returning(TicketDraft.id)
            )
            return len(result.all())

    # --- внутреннее ------------------------------------------------------------------------

    @staticmethod
    def _advance(draft: TicketDraft) -> None:
        draft.step = _NEXT_STEP[draft.step]
        draft.prompt_message_id = None
        draft.version = draft.version + 1

    @staticmethod
    async def _require_creator(session: AsyncSession, user_id: int) -> None:
        actor = await load_actor(session, user_id)
        if not can_create(actor.role):
            raise PermissionDeniedError("Создавать обращения могут только support и admin")

    def _active_query(self, user_id: int) -> Select[TicketDraft]:
        return (
            select(TicketDraft)
            .where(
                TicketDraft.user_id == user_id,
                TicketDraft.updated_at > func.now() - literal(self._ttl, Interval()),
            )
            .execution_options(populate_existing=True)
        )

    async def _get_active(self, session: AsyncSession, user_id: int) -> TicketDraft | None:
        return (await session.execute(self._active_query(user_id))).scalar_one_or_none()

    async def _lock_active(self, session: AsyncSession, user_id: int) -> TicketDraft | None:
        stmt = self._active_query(user_id).with_for_update()
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _lock_for_callback(
        self,
        session: AsyncSession,
        user_id: int,
        draft_id: uuid.UUID,
        version: int,
        step: DraftStep | None = None,
    ) -> TicketDraft:
        draft = await self._lock_active(session, user_id)
        if draft is None:
            raise DraftNotFoundError()
        if draft.id != draft_id or draft.version != version or (step and draft.step != step):
            raise DraftStaleError(DraftSnapshot.from_model(draft))
        return draft

    @staticmethod
    async def _create(session: AsyncSession, user_id: int) -> DraftSnapshot:
        # Не более одного черновика на пользователя: старый (в т.ч. просроченный) удаляем.
        await session.execute(delete(TicketDraft).where(TicketDraft.user_id == user_id))
        draft = TicketDraft(id=uuid.uuid4(), user_id=user_id, step=DraftStep.DESCRIPTION, version=1)
        session.add(draft)
        await session.flush()
        return DraftSnapshot.from_model(draft)
