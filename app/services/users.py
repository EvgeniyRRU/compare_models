"""Пользователи: upsert по telegram_id и смена ролей."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Ticket, User
from app.services.errors import ActiveTicketsError, NotFoundError

EXECUTOR_ROLES = ("developer", "admin")
ALL_ROLES = ("support", "developer", "admin")
ACTIVE_STATUSES = ("in_progress", "waiting_info")


async def upsert_user(
    session: AsyncSession,
    *,
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> User:
    """Создаёт пользователя или обновляет username/имя при каждом взаимодействии."""
    user = (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if user is None:
        user = User(
            telegram_id=telegram_id, username=username, first_name=first_name, last_name=last_name
        )
        session.add(user)
        await session.flush()
        return user
    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    await session.flush()
    return user


async def change_role(session: AsyncSession, *, user_id: int, new_role: str | None) -> User:
    """Смена роли.

    Строка пользователя блокируется FOR UPDATE; взятие и переназначение берут
    FOR SHARE строку исполнителя — проверка «нет активных обращений» не может
    быть обойдена параллельным взятием (SPEC «Роли»).
    """
    user = (
        await session.execute(select(User).where(User.id == user_id).with_for_update())
    ).scalar_one_or_none()
    if user is None:
        raise NotFoundError("Пользователь не найден.")
    if new_role is not None and new_role not in ALL_ROLES:
        raise ValueError(f"Некорректная роль: {new_role}")
    if new_role not in EXECUTOR_ROLES:
        active = (
            (
                await session.execute(
                    select(Ticket.id)
                    .where(Ticket.assignee_id == user_id, Ticket.status.in_(ACTIVE_STATUSES))
                    .order_by(Ticket.id)
                )
            )
            .scalars()
            .all()
        )
        if active:
            raise ActiveTicketsError(list(active))
    user.role = new_role
    await session.flush()
    return user
