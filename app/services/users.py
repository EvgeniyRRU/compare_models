from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Role, User
from app.logging_setup import get_logger

log = get_logger("services.users")


async def ensure_user(
    session: AsyncSession, telegram_id: int, username: str | None, full_name: str | None
) -> User:
    """Register if unknown; refresh username/full_name on every interaction.

    Never caches roles: permissions are always re-read from DB by callers.
    """
    await session.execute(
        insert(User)
        .values(telegram_id=telegram_id, username=username, full_name=full_name or "")
        .on_conflict_do_nothing(index_elements=["telegram_id"])
    )
    user = await session.get(User, telegram_id)
    assert user is not None
    changed = False
    if username is not None and user.username != username:
        user.username = username
        changed = True
    if full_name and user.full_name != full_name:
        user.full_name = full_name
        changed = True
    if changed:
        await session.flush()
    return user


async def apply_bootstrap_role(session: AsyncSession, user: User, settings: Settings) -> User:
    """Primary admins from BOOTSTRAP_ADMIN_IDS get the admin role on first /start."""
    if user.role is None and user.telegram_id in settings.bootstrap_admin_ids:
        user.role = Role.admin
        await session.flush()
        log.info(
            "bootstrap_admin_granted",
            component="users",
            telegram_user_id=user.telegram_id,
        )
    return user


async def get_role(session: AsyncSession, telegram_id: int) -> Role | None:
    """Current role from DB — authority for every check, no caching."""
    return await session.scalar(select(User.role).where(User.telegram_id == telegram_id))
