"""Current database identities and serialized role administration."""

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from support_bot.config import Settings
from support_bot.db.models import Ticket, User
from support_bot.domain.errors import BusinessError, Forbidden, NotFound
from support_bot.domain.transitions import ACTIVE


async def current_user(session: AsyncSession, user_id: int, *, lock: bool = True) -> User:
    query = select(User).where(User.id == user_id)
    if lock:
        query = query.with_for_update(read=True)
    user = await session.scalar(query)
    if user is None:
        raise NotFound("Пользователь не найден")
    return user


class UserService:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], settings: Settings):
        self.sessions = sessions
        self.settings = settings

    async def touch(
        self,
        telegram_id: int,
        first_name: str,
        username: str | None = None,
        last_name: str | None = None,
        bootstrap: bool = False,
    ) -> User:
        admin = bootstrap and telegram_id in self.settings.bootstrap_admin_ids
        statement = insert(User).values(
            telegram_id=telegram_id,
            first_name=first_name,
            username=username,
            last_name=last_name,
            role="admin" if admin else None,
            started_at=func.now() if bootstrap else None,
        )
        changes: dict[str, Any] = dict(
            first_name=first_name, username=username, last_name=last_name
        )
        if bootstrap:
            changes["started_at"] = func.coalesce(User.started_at, func.now())
            if admin:
                changes["role"] = case((User.started_at.is_(None), "admin"), else_=User.role)
        returning_statement = statement.on_conflict_do_update(
            index_elements=[User.telegram_id],
            set_=changes,
        ).returning(User)
        async with self.sessions.begin() as session:
            return (await session.scalars(returning_statement)).one()

    async def get(self, user_id: int) -> User:
        async with self.sessions.begin() as session:
            return await current_user(session, user_id, lock=False)

    async def set_role(self, actor_id: int, user_id: int, role: str | None) -> User:
        async with self.sessions.begin() as session:
            # Stable lock order also permits an administrator to demote themselves.
            users = {
                u.id: u
                for u in await session.scalars(
                    select(User)
                    .where(User.id.in_({actor_id, user_id}))
                    .order_by(User.id)
                    .with_for_update(),
                )
            }
            actor = users.get(actor_id)
            if actor is None or actor.role != "admin":
                raise Forbidden()
            target = users.get(user_id)
            if target is None:
                raise NotFound("Пользователь не найден")
            if role not in {None, "support", "developer", "admin"}:
                raise BusinessError("Неизвестная роль")
            if role not in {"developer", "admin"}:
                assigned = list(
                    await session.scalars(
                        select(Ticket.id)
                        .where(Ticket.assignee_id == user_id, Ticket.status.in_(ACTIVE))
                        .order_by(Ticket.id),
                    )
                )
                if assigned:
                    raise BusinessError(
                        "Сначала переназначьте активные обращения",
                        code="active_tickets",
                        ticket_ids=assigned,
                    )
            target.role = role
            await session.flush()
            await session.refresh(target)
            return target
