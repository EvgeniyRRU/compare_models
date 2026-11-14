from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.db.enums import ASSIGNED_STATUSES, UserRole
from support_bot.db.models.ticket import Ticket
from support_bot.db.models.user import User
from support_bot.domain.errors import PermissionDenied, RoleChangeConflict, UserNotFound


@dataclass(frozen=True, slots=True)
class TelegramProfile:
    telegram_id: int
    username: str | None
    first_name: str | None
    last_name: str | None


async def upsert_telegram_user(
    session: AsyncSession,
    profile: TelegramProfile,
    bootstrap_admin_ids: list[int],
) -> User:
    """Insert a user on first contact and refresh username/name on every contact."""
    initial_role = UserRole.ADMIN.value if profile.telegram_id in bootstrap_admin_ids else None
    stmt = (
        pg_insert(User)
        .values(
            telegram_id=profile.telegram_id,
            username=profile.username,
            first_name=profile.first_name,
            last_name=profile.last_name,
            role=initial_role,
        )
        .on_conflict_do_update(
            index_elements=[User.telegram_id],
            set_={
                "username": profile.username,
                "first_name": profile.first_name,
                "last_name": profile.last_name,
                "updated_at": func.now(),
            },
        )
        .returning(User)
    )
    async with session.begin():
        user = (await session.execute(stmt)).scalar_one()
    return user


async def get_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(select(User).where(User.telegram_id == telegram_id))


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    return await session.get(User, user_id)


async def get_role(
    session: AsyncSession, user_id: int, *, for_share: bool = False
) -> UserRole | None:
    stmt = select(User.role).where(User.id == user_id)
    if for_share:
        stmt = stmt.with_for_update(read=True)
    return await session.scalar(stmt)


async def set_role(
    session: AsyncSession,
    *,
    actor_id: int,
    target_id: int,
    new_role: UserRole | None,
) -> User:
    async with session.begin():
        actor_role = await get_role(session, actor_id)
        if actor_role != UserRole.ADMIN:
            raise PermissionDenied("admin role required")

        target = (
            await session.execute(select(User).where(User.id == target_id).with_for_update())
        ).scalar_one_or_none()
        if target is None:
            raise UserNotFound(str(target_id))

        if target.role in (UserRole.DEVELOPER, UserRole.ADMIN) and new_role not in (
            UserRole.DEVELOPER,
            UserRole.ADMIN,
        ):
            active_ids = list(
                (
                    await session.scalars(
                        select(Ticket.id)
                        .where(
                            Ticket.assignee_id == target_id,
                            Ticket.status.in_(ASSIGNED_STATUSES),
                        )
                        .order_by(Ticket.id)
                    )
                ).all()
            )
            if active_ids:
                raise RoleChangeConflict(active_ids)

        target.role = new_role
    return target


async def list_users(
    session: AsyncSession,
    *,
    query: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[User], int]:
    conditions = []
    if query:
        pattern = f"%{query.strip()}%"
        conditions.append(
            or_(
                User.username.ilike(pattern),
                User.first_name.ilike(pattern),
                User.last_name.ilike(pattern),
                cast(User.telegram_id, String).ilike(pattern),
            )
        )
    count_stmt = select(func.count()).select_from(User)
    stmt = select(User)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
        stmt = stmt.where(*conditions)
    total = int(await session.scalar(count_stmt) or 0)
    stmt = stmt.order_by(User.id).offset((page - 1) * page_size).limit(page_size)
    users = list((await session.scalars(stmt)).all())
    return users, total


async def list_assignable(session: AsyncSession) -> list[User]:
    return list(
        (
            await session.scalars(
                select(User)
                .where(User.role.in_([UserRole.DEVELOPER, UserRole.ADMIN]))
                .order_by(User.first_name, User.id)
            )
        ).all()
    )
