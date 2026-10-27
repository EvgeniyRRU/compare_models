"""Database-backed forms with versioned buttons and exact reply binding."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.db.models import Ticket, TicketDraft, User
from support_bot.domain.errors import BusinessError, Forbidden, NotFound
from support_bot.services.tickets import PRIORITIES, TicketService, validated_text


def stale_draft() -> BusinessError:
    return BusinessError("Форма изменилась. Продолжите с текущего шага", code="stale_draft")


class DraftService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings):
        self.sessions = sessions
        self.settings = settings
        self.tickets = TicketService(sessions, settings)

    def _cutoff(self) -> datetime:
        return datetime.now(UTC) - timedelta(hours=self.settings.draft_ttl_hours)

    async def _authorize(
        self, session: AsyncSession, user_id: int, *, exclusive: bool = False
    ) -> None:
        user = await session.scalar(
            select(User).where(User.id == user_id).with_for_update(read=not exclusive),
        )
        if user is None:
            raise NotFound("Пользователь не найден")
        if user.role not in {"support", "admin"}:
            raise Forbidden()

    async def _load(self, session: AsyncSession, user_id: int) -> TicketDraft | None:
        draft = await session.scalar(
            select(TicketDraft).where(TicketDraft.user_id == user_id).with_for_update(),
        )
        if draft is not None and draft.updated_at < self._cutoff():
            await session.delete(draft)
            await session.flush()
            return None
        return draft

    async def get(self, user_id: int) -> TicketDraft | None:
        async with self.sessions.begin() as session:
            await self._authorize(session, user_id)
            return await self._load(session, user_id)

    async def begin(self, user_id: int, restart: bool = False) -> TicketDraft:
        async with self.sessions.begin() as session:
            # User lock serializes creation even when no draft row exists yet.
            await self._authorize(session, user_id, exclusive=True)
            draft = await self._load(session, user_id)
            if draft is not None and not restart:
                return draft
            if draft is not None:
                await session.delete(draft)
                await session.flush()
            draft = TicketDraft(user_id=user_id, step="description", version=1)
            session.add(draft)
            await session.flush()
            return draft

    async def text(self, user_id: int, reply_message_id: int | None, value: str) -> TicketDraft:
        async with self.sessions.begin() as session:
            await self._authorize(session, user_id)
            draft = await self._load(session, user_id)
            if (
                draft is None
                or reply_message_id is None
                or draft.prompt_message_id is None
                or reply_message_id != draft.prompt_message_id
                or draft.step not in {"description", "request_id", "tenant_id"}
            ):
                raise stale_draft()
            step = draft.step
            limit = 3000 if step == "description" else 64
            value = validated_text(
                value,
                limit,
                {
                    "description": "Описание",
                    "request_id": "ID заявки",
                    "tenant_id": "ID арендатора",
                }[step],
            )
            setattr(draft, step, value)
            draft.step = {
                "description": "priority",
                "request_id": "tenant_id",
                "tenant_id": "confirm",
            }[step]
            return await self._advance(session, draft)

    async def _advance(self, session: AsyncSession, draft: TicketDraft) -> TicketDraft:
        draft.version += 1
        draft.prompt_message_id = None
        await session.flush()
        await session.refresh(draft)
        return draft

    async def action(
        self,
        user_id: int,
        draft_id: UUID,
        version: int,
        action: str,
    ) -> TicketDraft | Ticket | None:
        # Exclusive user lock also serializes restart against concurrent /new.
        async with self.sessions.begin() as session:
            await self._authorize(session, user_id, exclusive=True)
            draft = await self._load(session, user_id)
            if draft is None or draft.id != draft_id or draft.version != version:
                raise stale_draft()
            if action == "continue":
                return draft
            if action == "cancel":
                await session.delete(draft)
                return None
            if action == "restart":
                await session.delete(draft)
                await session.flush()
                replacement = TicketDraft(user_id=user_id, step="description", version=1)
                session.add(replacement)
                await session.flush()
                return replacement
            if action.startswith("p") and action[1:] in PRIORITIES and draft.step == "priority":
                draft.priority = action[1:]
                draft.step = "request_id"
            elif action == "skip" and draft.step == "tenant_id":
                draft.tenant_id = None
                draft.step = "confirm"
            elif action == "submit" and draft.step == "confirm":
                if draft.description is None or draft.priority is None or draft.request_id is None:
                    raise BusinessError("Заполните обязательные поля формы")
                ticket = await self.tickets.create_in(
                    session,
                    user_id,
                    draft.description,
                    draft.priority,
                    draft.request_id,
                    draft.tenant_id,
                )
                await session.delete(draft)
                await session.flush()
                return ticket
            else:
                raise BusinessError("Действие недоступно на текущем шаге")
            return await self._advance(session, draft)

    async def save_prompt(self, draft_id: UUID, version: int, step: str, message_id: int) -> bool:
        async with self.sessions.begin() as session:
            result = await session.scalar(
                update(TicketDraft)
                .where(
                    TicketDraft.id == draft_id,
                    TicketDraft.version == version,
                    TicketDraft.step == step,
                    TicketDraft.updated_at >= self._cutoff(),
                )
                .values(prompt_message_id=message_id)
                .returning(TicketDraft.id)
            )
            return result is not None

    async def cancel(self, user_id: int) -> None:
        async with self.sessions.begin() as session:
            await self._authorize(session, user_id)
            # Lock before deletion to serialize against an in-flight answer or submit.
            draft = await self._load(session, user_id)
            if draft is not None:
                await session.delete(draft)
