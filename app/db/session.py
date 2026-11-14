"""Async engine and session factory built from :data:`app.config.settings`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a scoped async session."""
    async with async_session_factory() as session:
        yield session


class AdvisoryLockTimeout(Exception):
    """Raised when a blocking advisory lock cannot be acquired in time."""

    def __init__(self, key: int) -> None:
        self.key = key
        super().__init__(f"Таймаут блокировки обращения #{key}")


#: PostgreSQL SQLSTATE raised when ``lock_timeout`` expires while waiting on a
#: lock (``lock_not_available``).
_LOCK_TIMEOUT_SQLSTATE = "55P03"


@asynccontextmanager
async def advisory_lock(
    engine: AsyncEngine,
    key: int,
    *,
    timeout: float | None = None,
    blocking: bool = True,
) -> AsyncIterator[AsyncConnection | None]:
    """Acquire a session-level advisory lock on a dedicated connection.

    ``blocking=True`` runs ``SELECT pg_advisory_lock(:key)`` bounded by
    ``SET LOCAL lock_timeout``; raises :class:`AdvisoryLockTimeout` on timeout.
    ``blocking=False`` runs ``SELECT pg_try_advisory_lock(:key)``; if the lock
    is not acquired, the context yields ``None`` (caller skips).

    The lock is released via ``pg_advisory_unlock`` in a ``finally``; if the
    release raises, the connection is invalidated so it is not returned to the
    pool while still holding the lock.
    """
    acquired = False
    async with engine.connect() as conn:
        try:
            if blocking:
                if timeout is not None:
                    # ``SET`` cannot take a bind parameter; inline the safe
                    # integer millisecond value computed from ``timeout``. Clamp
                    # to >= 1ms: PostgreSQL treats ``lock_timeout = 0`` as "no
                    # timeout", which would invert the caller's intent.
                    ms = max(1, int(timeout * 1000))
                    await conn.execute(text(f"SET LOCAL lock_timeout = {ms}"))
                try:
                    await conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
                except DBAPIError as exc:
                    if getattr(exc.orig, "sqlstate", None) == _LOCK_TIMEOUT_SQLSTATE:
                        raise AdvisoryLockTimeout(key) from exc
                    raise
                acquired = True
                yield conn
            else:
                held = (
                    await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
                ).scalar_one()
                if not held:
                    yield None
                else:
                    acquired = True
                    yield conn
        finally:
            if acquired:
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                except Exception:
                    await conn.invalidate()
                    raise
