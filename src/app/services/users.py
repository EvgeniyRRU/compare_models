from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Ticket, User
from app.domain.state_machine import (
    ACTIVE_STATUSES,
    Actor,
    NotFound,
    PermissionDenied,
    Role,
    TicketError,
    can_be_assignee,
)


class RoleChangeBlocked(TicketError):
    def __init__(self, ticket_ids: list[int]):
        self.ticket_ids = ticket_ids
        super().__init__(
            "У пользователя есть активные обращения: "
            + ", ".join(f"#{i}" for i in ticket_ids)
            + ". Сначала переназначьте их."
        )


async def ensure_user(
    session: AsyncSession,
    telegram_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    bootstrap_admin_ids: Sequence[int] = (),
) -> User:
    """Регистрирует/обновляет пользователя. Роль admin из BOOTSTRAP_ADMIN_IDS — только при создании."""
    initial_role = Role.ADMIN.value if telegram_id in bootstrap_admin_ids else None
    stmt = insert(User).values(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        role=initial_role,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[User.telegram_id],
        set_={
            "username": stmt.excluded.username,
            "first_name": stmt.excluded.first_name,
            "last_name": stmt.excluded.last_name,
        },
    ).returning(User)
    user = (await session.execute(stmt.execution_options(populate_existing=True))).scalar_one()
    return user


async def get_actor(session: AsyncSession, user_id: int, *, lock_share: bool = False) -> Actor:
    """Актуальная роль из БД (без кеша). lock_share: FOR SHARE для взятия/переназначения."""
    stmt = select(User.id, User.role).where(User.id == user_id)
    if lock_share:
        stmt = stmt.with_for_update(read=True)
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise NotFound("Пользователь не найден")
    return Actor(id=row.id, role=Role(row.role) if row.role else None)


async def set_role(
    session: AsyncSession, actor_id: int, target_user_id: int, new_role: Role | None
) -> User:
    actor = await get_actor(session, actor_id)
    if actor.role != Role.ADMIN:
        raise PermissionDenied("Управлять ролями может только админ")
    target = (
        await session.execute(select(User).where(User.id == target_user_id).with_for_update())
    ).scalar_one_or_none()
    if target is None:
        raise NotFound("Пользователь не найден")
    old_role = Role(target.role) if target.role else None
    if can_be_assignee(old_role) and not can_be_assignee(new_role):
        ids = (
            await session.execute(
                select(Ticket.id)
                .where(Ticket.assignee_id == target.id, Ticket.status.in_(ACTIVE_STATUSES))
                .order_by(Ticket.id)
            )
        ).scalars().all()
        if ids:
            raise RoleChangeBlocked(list(ids))
    target.role = new_role.value if new_role else None
    await session.flush()
    return target


async def list_users(session: AsyncSession, role: Role | None = None) -> list[User]:
    stmt = select(User).order_by(User.id)
    if role:
        stmt = stmt.where(User.role == role.value)
    return list((await session.execute(stmt)).scalars())


async def count_users(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(User))).scalar_one()
