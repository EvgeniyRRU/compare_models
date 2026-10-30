import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_LOCK_NAMESPACE = 0x5550_0000_0000_0000


def create_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_size=10, max_overflow=10, pool_pre_ping=True)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


class LockTimeout(Exception):
    pass


@contextlib.asynccontextmanager
async def advisory_lock(
    engine: AsyncEngine,
    key: int,
    wait: float,
    *,
    blocking: bool = True,
) -> AsyncIterator[bool]:
    """Session-level advisory lock на выделенном соединении.

    Блокировка живёт до явного `pg_advisory_unlock` или конца сессии. Если снять её не
    удалось, соединение закрывается, а не возвращается в пул.
    blocking=False: при занятой блокировке выдаёт False (для фоновых задач).
    blocking=True: ждёт до timeout, затем LockTimeout.
    """
    lock_key = _LOCK_NAMESPACE + key
    conn = await engine.connect()
    acquired = False
    try:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        deadline = time.monotonic() + wait
        while True:
            acquired = bool(
                (
                    await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key})
                ).scalar()
            )
            if acquired or not blocking:
                break
            if time.monotonic() >= deadline:
                raise LockTimeout(f"advisory lock {key} timeout")
            await asyncio.sleep(0.05)
        yield acquired
    finally:
        unlock_ok = True
        if acquired:
            try:
                await asyncio.shield(
                    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
                )
            except BaseException:
                unlock_ok = False
        try:
            if unlock_ok:
                await asyncio.shield(conn.close())
            else:
                await asyncio.shield(conn.invalidate())
                await asyncio.shield(conn.close())
        except BaseException:
            pass
