from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ACTIVE_STATUSES, Role, Ticket, User
from app.domain.errors import Forbidden, NotFound, RoleChangeBlocked
from app.logging_setup import get_logger

log = get_logger("services.roles")


async def set_role(
    session: AsyncSession, actor_id: int, target_id: int, new_role: Role | None
) -> User:
    """Admin-only role assignment.

    A developer/admin cannot be demoted to a role without executor rights while
    tickets are active on them — admin must reassign first. The target row is
    locked FOR UPDATE here, while take/reassign lock it FOR SHARE, so the check
    cannot be bypassed by a concurrent take.
    """
    actor_role = await session.scalar(select(User.role).where(User.telegram_id == actor_id))
    if actor_role is not Role.admin:
        raise Forbidden("Назначать роли может только админ")

    target = (
        await session.execute(
            select(User).where(User.telegram_id == target_id).with_for_update()
        )
    ).scalar_one_or_none()
    if target is None:
        raise NotFound("Пользователь не найден")

    removing_executor_rights = target.role is not None and target.role.can_execute and (
        new_role is None or not new_role.can_execute
    )
    if removing_executor_rights:
        active = list(
            await session.scalars(
                select(Ticket.id).where(
                    Ticket.assignee_id == target_id, Ticket.status.in_(ACTIVE_STATUSES)
                )
            )
        )
        if active:
            raise RoleChangeBlocked(active)

    target.role = new_role
    await session.flush()
    log.info(
        "role_changed",
        component="roles",
        actor_id=actor_id,
        telegram_user_id=target_id,
        role=new_role.value if new_role else None,
    )
    return target


async def users_with_executor_role(session: AsyncSession, query: str | None = None) -> list[User]:
    """Candidates for reassignment: developer or admin."""
    stmt = select(User).where(User.role.in_([Role.developer, Role.admin]))
    if query:
        like = f"%{query.lower()}%"
        stmt = stmt.where(
            func.lower(func.coalesce(User.username, "")).like(like)
            | func.lower(User.full_name).like(like)
        )
    return list(await session.scalars(stmt.order_by(User.full_name)))
