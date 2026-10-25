from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.db.engine import SessionFactory
from support_bot.db.models import Ticket, User
from support_bot.domain.enums import ACTIVE_STATUSES, Role
from support_bot.domain.errors import (
    NotFoundError,
    PermissionDeniedError,
    RoleChangeBlockedError,
    ValidationFailedError,
)
from support_bot.domain.state_machine import can_be_assignee, is_admin, require_role
from support_bot.services.views import UserInfo


@dataclass(frozen=True)
class TelegramProfile:
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None


async def load_user(session: AsyncSession, user_id: int, *, lock: str | None = None) -> User | None:
    """Прочитать пользователя в текущей транзакции. lock: None | "share" | "update"."""
    stmt = select(User).where(User.id == user_id).execution_options(populate_existing=True)
    if lock == "share":
        stmt = stmt.with_for_update(read=True)
    elif lock == "update":
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def load_actor(session: AsyncSession, user_id: int, *, lock: str | None = None) -> User:
    """Актор операции с актуальной ролью из БД (права не кешируются)."""
    user = await load_user(session, user_id, lock=lock)
    if user is None:
        raise NotFoundError("Пользователь не найден")
    require_role(user.role)
    return user


class UserService:
    def __init__(self, session_factory: SessionFactory, bootstrap_admin_ids: Iterable[int]) -> None:
        self._sf = session_factory
        self._bootstrap_admin_ids = frozenset(bootstrap_admin_ids)

    async def touch(self, profile: TelegramProfile) -> UserInfo:
        """Зарегистрировать пользователя без роли или обновить username и имя."""
        async with self._sf.begin() as session:
            return UserInfo.from_model(await self._upsert(session, profile))

    async def register_start(self, profile: TelegramProfile) -> UserInfo:
        """/start: регистрация; первичные админы получают роль, если роли ещё нет."""
        async with self._sf.begin() as session:
            user = await self._upsert(session, profile)
            if user.role is None and user.telegram_id in self._bootstrap_admin_ids:
                result = await session.execute(
                    update(User)
                    .where(User.id == user.id, User.role.is_(None))
                    .values(role=Role.ADMIN)
                    .returning(User)
                    .execution_options(populate_existing=True)
                )
                user = result.scalar_one_or_none() or user
            return UserInfo.from_model(user)

    async def _upsert(self, session: AsyncSession, profile: TelegramProfile) -> User:
        stmt = pg_insert(User).values(
            telegram_id=profile.telegram_id,
            username=profile.username,
            first_name=profile.first_name,
            last_name=profile.last_name,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[User.telegram_id],
            set_={
                "username": stmt.excluded.username,
                "first_name": stmt.excluded.first_name,
                "last_name": stmt.excluded.last_name,
            },
            # Пишем только при реальном изменении, чтобы не трогать строку на каждом апдейте.
            where=or_(
                User.username.is_distinct_from(stmt.excluded.username),
                User.first_name.is_distinct_from(stmt.excluded.first_name),
                User.last_name.is_distinct_from(stmt.excluded.last_name),
            ),
        )
        result = await session.execute(
            stmt.returning(User).execution_options(populate_existing=True)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = (
                await session.execute(
                    select(User)
                    .where(User.telegram_id == profile.telegram_id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
        return user

    async def get(self, user_id: int) -> UserInfo | None:
        async with self._sf() as session:
            user = await load_user(session, user_id)
            return UserInfo.from_model(user) if user else None

    async def list_users(self, actor_id: int) -> list[UserInfo]:
        async with self._sf() as session:
            await self._require_admin(session, actor_id)
            users = (await session.scalars(select(User).order_by(User.id))).all()
            return [UserInfo.from_model(u) for u in users]

    async def list_assignees(self, actor_id: int) -> list[UserInfo]:
        async with self._sf() as session:
            await self._require_admin(session, actor_id)
            users = (
                await session.scalars(
                    select(User)
                    .where(User.role.in_([Role.DEVELOPER, Role.ADMIN]))
                    .order_by(User.id)
                )
            ).all()
            return [UserInfo.from_model(u) for u in users]

    async def set_role(self, actor_id: int, target_id: int, new_role: Role | None) -> UserInfo:
        async with self._sf.begin() as session:
            await self._require_admin(session, actor_id)
            if actor_id == target_id:
                raise ValidationFailedError("Нельзя менять роль самому себе")

            preview = await load_user(session, target_id)
            if preview is not None and preview.role is Role.ADMIN:
                # Строки админов блокируем заранее и в порядке id, чтобы встречные
                # понижения двух админов не взаимоблокировались.
                await self._lock_admins(session)
            # FOR UPDATE: параллельное взятие/переназначение (FOR SHARE на исполнителе)
            # дождётся этой транзакции или будет учтено проверкой ниже.
            target = await load_user(session, target_id, lock="update")
            if target is None:
                raise NotFoundError("Пользователь не найден")
            if target.role == new_role:
                return UserInfo.from_model(target)

            if can_be_assignee(target.role) and not can_be_assignee(new_role):
                active = (
                    await session.scalars(
                        select(Ticket.id)
                        .where(Ticket.assignee_id == target.id, Ticket.status.in_(ACTIVE_STATUSES))
                        .order_by(Ticket.id)
                    )
                ).all()
                if active:
                    raise RoleChangeBlockedError(list(active))

            if (
                target.role is Role.ADMIN
                and new_role is not Role.ADMIN
                and len(await self._lock_admins(session)) <= 1
            ):
                raise ValidationFailedError("Нельзя снять роль с последнего администратора")

            target.role = new_role
            await session.flush()
            return UserInfo.from_model(target)

    @staticmethod
    async def _lock_admins(session: AsyncSession) -> list[int]:
        rows = await session.scalars(
            select(User.id).where(User.role == Role.ADMIN).order_by(User.id).with_for_update()
        )
        return list(rows)

    @staticmethod
    async def _require_admin(session: AsyncSession, actor_id: int) -> User:
        actor = await load_actor(session, actor_id)
        if not is_admin(actor.role):
            raise PermissionDeniedError()
        return actor
