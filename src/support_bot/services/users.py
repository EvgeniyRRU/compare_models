from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_bot.config import Settings
from support_bot.db.models import Ticket, User
from support_bot.domain.errors import Forbidden, NotFound, RoleChangeBlocked
from support_bot.domain.roles import Role


async def register_user(
    session: AsyncSession,
    *,
    settings: Settings,
    telegram_id: int,
    username: str | None,
    full_name: str,
) -> User:
    """Create the user on first contact and refresh username and name on every call.

    Bootstrap admins get the admin role only when the row is created.
    """
    role = Role.ADMIN.value if telegram_id in settings.bootstrap_admin_ids else None
    stmt = (
        insert(User)
        .values(telegram_id=telegram_id, username=username, full_name=full_name, role=role)
        .on_conflict_do_update(
            index_elements=[User.telegram_id],
            set_={"username": username, "full_name": full_name},
        )
        .returning(User.id)
    )
    user_id = await session.scalar(stmt)
    user = await session.get(User, user_id, populate_existing=True)
    assert user is not None
    return user


async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    return await session.scalar(
        select(User)
        .where(User.telegram_id == telegram_id)
        .execution_options(populate_existing=True)
    )


async def current_role(
    session: AsyncSession, user_id: int, *, share_lock: bool = False
) -> Role | None:
    """Read the role from the database, never from a cache.

    `share_lock` takes FOR SHARE on the user row, so a concurrent role change waits
    until the caller's transaction ends.
    """
    stmt = select(User.role).where(User.id == user_id)
    if share_lock:
        stmt = stmt.with_for_update(read=True)
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise NotFound("Пользователь не найден")
    return Role(row.role) if row.role is not None else None


async def set_role(
    session: AsyncSession,
    *,
    admin_id: int,
    target_telegram_id: int,
    role: Role | None,
) -> User:
    if await current_role(session, admin_id, share_lock=True) is not Role.ADMIN:
        raise Forbidden("Менять роли может только администратор")

    target = await session.scalar(
        select(User).where(User.telegram_id == target_telegram_id).with_for_update()
    )
    if target is None:
        raise NotFound("Пользователь не найден")

    was_assignee = target.role in (Role.DEVELOPER.value, Role.ADMIN.value)
    becomes_assignee = role is not None and role.can_be_assignee
    if was_assignee and not becomes_assignee:
        busy = (
            await session.scalars(
                select(Ticket.id)
                .where(
                    Ticket.assignee_id == target.id,
                    Ticket.status.in_(("in_progress", "waiting_info")),
                )
                .order_by(Ticket.id)
            )
        ).all()
        if busy:
            raise RoleChangeBlocked(list(busy))

    await session.execute(
        update(User).where(User.id == target.id).values(role=role.value if role else None)
    )
    return await session.get(User, target.id, populate_existing=True) or target
