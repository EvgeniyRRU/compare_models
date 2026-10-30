import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, TicketDraft
from app.domain.state_machine import Priority
from app.services.tickets import NewTicket, TicketService

DESCRIPTION_MAX = 3000
ID_MAX = 64


class Step(StrEnum):
    DESCRIPTION = "desc"
    PRIORITY = "prio"
    REQUEST_ID = "req"
    TENANT_ID = "tenant"
    CONFIRM = "confirm"


TEXT_STEPS = frozenset({Step.DESCRIPTION, Step.REQUEST_ID, Step.TENANT_ID})
_NEXT = {
    Step.DESCRIPTION: Step.PRIORITY,
    Step.PRIORITY: Step.REQUEST_ID,
    Step.REQUEST_ID: Step.TENANT_ID,
    Step.TENANT_ID: Step.CONFIRM,
}
_FIELD = {
    Step.DESCRIPTION: "description",
    Step.REQUEST_ID: "request_id",
    Step.TENANT_ID: "tenant_id",
}
_LIMITS = {Step.DESCRIPTION: DESCRIPTION_MAX, Step.REQUEST_ID: ID_MAX, Step.TENANT_ID: ID_MAX}


class TextResult(StrEnum):
    ACCEPTED = "accepted"
    INVALID = "invalid"  # не прошло валидацию: данные/шаг/version не менялись
    UNBOUND = "unbound"  # не reply на текущий вопрос: заново задать вопрос
    NO_DRAFT = "no_draft"


class ActionResult(StrEnum):
    OK = "ok"
    STALE = "stale"  # чужой/удалённый черновик или устаревшая версия
    SUBMITTED = "submitted"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class DraftView:
    id: uuid.UUID
    step: Step
    version: int
    data: dict[str, str]
    prompt_message_id: int | None


def _view(d: TicketDraft) -> DraftView:
    return DraftView(d.id, Step(d.step), d.version, dict(d.data), d.prompt_message_id)


def encode_id(draft_id: uuid.UUID) -> str:
    import base64

    return base64.urlsafe_b64encode(draft_id.bytes).decode().rstrip("=")


def decode_id(s: str) -> uuid.UUID | None:
    import base64

    try:
        return uuid.UUID(bytes=base64.urlsafe_b64decode(s + "=="))
    except ValueError:
        return None


def validate_text(step: Step, text: str) -> str | None:
    """Возвращает текст ошибки или None."""
    value = text.strip()
    if not value:
        return "Значение не может быть пустым."
    limit = _LIMITS[step]
    if len(value) > limit:
        return f"Слишком длинно: {len(value)} из {limit} символов."
    return None


class DraftService:
    def __init__(self, tickets: TicketService, ttl: timedelta):
        self._tickets = tickets
        self._ttl = ttl

    async def get_active(self, session: AsyncSession, user_id: int) -> DraftView | None:
        d = (
            await session.execute(select(TicketDraft).where(TicketDraft.user_id == user_id))
        ).scalar_one_or_none()
        return _view(d) if d else None

    async def start(self, session: AsyncSession, user_id: int) -> DraftView:
        """Новый черновик (с новым UUID); прежний, если был, удаляется."""
        await session.execute(delete(TicketDraft).where(TicketDraft.user_id == user_id))
        d = TicketDraft(user_id=user_id, step=Step.DESCRIPTION.value, data={}, version=1)
        session.add(d)
        await session.flush()
        return _view(d)

    async def cancel(self, session: AsyncSession, user_id: int) -> bool:
        res = await session.execute(delete(TicketDraft).where(TicketDraft.user_id == user_id))
        return res.rowcount > 0  # type: ignore[attr-defined,no-any-return]

    async def _lock(self, session: AsyncSession, user_id: int) -> TicketDraft | None:
        return (
            await session.execute(
                select(TicketDraft).where(TicketDraft.user_id == user_id).with_for_update()
            )
        ).scalar_one_or_none()

    async def submit_text(
        self, session: AsyncSession, user_id: int, text: str, reply_to_message_id: int | None
    ) -> tuple[TextResult, DraftView | None, str | None]:
        d = await self._lock(session, user_id)
        if d is None:
            return TextResult.NO_DRAFT, None, None
        step = Step(d.step)
        if (
            step not in TEXT_STEPS
            or d.prompt_message_id is None
            or reply_to_message_id != d.prompt_message_id
        ):
            return TextResult.UNBOUND, _view(d), None
        error = validate_text(step, text)
        if error:
            return TextResult.INVALID, _view(d), error
        self._advance(d, text.strip())
        await session.flush()
        return TextResult.ACCEPTED, _view(d), None

    def _advance(self, d: TicketDraft, value: str | None) -> None:
        step = Step(d.step)
        data = dict(d.data)
        if value is not None:
            data[_FIELD.get(step, step.value)] = value
        d.data = data
        d.step = _NEXT[step].value
        d.prompt_message_id = None
        d.version += 1

    async def press(
        self,
        session: AsyncSession,
        user_id: int,
        draft_id: uuid.UUID,
        version: int,
        action: str,
        value: str | None = None,
    ) -> tuple[ActionResult, DraftView | None, Ticket | None]:
        """Кнопки формы: p (приоритет), s (пропуск арендатора), ok, x (отмена), r (начать заново)."""
        d = await self._lock(session, user_id)
        if d is None or d.id != draft_id or d.version != version:
            return ActionResult.STALE, (_view(d) if d else None), None
        step = Step(d.step)
        if action == "x":
            await session.delete(d)
            await session.flush()
            return ActionResult.CANCELLED, None, None
        if action == "c":  # «Продолжить» — повторный показ текущего шага
            return ActionResult.OK, _view(d), None
        if action == "r":
            await session.delete(d)
            await session.flush()
            return ActionResult.OK, await self.start(session, user_id), None
        if action == "p" and step == Step.PRIORITY and value in {p.value for p in Priority}:
            assert value is not None
            d.data = {**d.data, "priority": value}
            self._advance(d, None)
            await session.flush()
            return ActionResult.OK, _view(d), None
        if action == "s" and step == Step.TENANT_ID:
            self._advance(d, None)
            await session.flush()
            return ActionResult.OK, _view(d), None
        if action == "ok" and step == Step.CONFIRM:
            data = d.data
            await session.delete(d)
            await session.flush()
            ticket = await self._tickets.create(
                session,
                user_id,
                NewTicket(
                    description=data["description"],
                    priority=Priority(data["priority"]),
                    request_id=data["request_id"],
                    tenant_id=data.get("tenant_id"),
                ),
            )
            return ActionResult.SUBMITTED, None, ticket
        return ActionResult.STALE, _view(d), None

    async def set_prompt_message_id(
        self, session: AsyncSession, draft_id: uuid.UUID, version: int, step: Step, message_id: int
    ) -> bool:
        """Техническая запись: version не меняется; не перезаписывает, если черновик ушёл вперёд."""
        res = await session.execute(
            update(TicketDraft)
            .where(
                TicketDraft.id == draft_id,
                TicketDraft.version == version,
                TicketDraft.step == step.value,
            )
            .values(prompt_message_id=message_id)
        )
        return res.rowcount > 0  # type: ignore[attr-defined,no-any-return]

    async def cleanup_expired(self, session: AsyncSession) -> int:
        res = await session.execute(
            delete(TicketDraft).where(TicketDraft.updated_at < func.now() - self._ttl)
        )
        return res.rowcount  # type: ignore[attr-defined,no-any-return]
