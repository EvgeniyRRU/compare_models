"""Session-level advisory lock PostgreSQL на выделенном физическом соединении.

Блокировка живёт до явного pg_advisory_unlock или конца сессии БД: возврат соединения
в пул её НЕ снимает. Поэтому снятие выполняется в finally (в т.ч. при ошибке, таймауте и
отмене задачи), а если снять не удалось или состояние неизвестно, соединение
инвалидируется (закрывается), и PostgreSQL снимает блокировку вместе с сессией.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = structlog.get_logger(__name__)

_TRY_LOCK = text("SELECT pg_try_advisory_lock(:key)")
_UNLOCK = text("SELECT pg_advisory_unlock(:key)")


class LockNotAcquiredError(Exception):
    pass


async def _try_lock(conn: AsyncConnection, key: int) -> bool:
    return bool((await conn.execute(_TRY_LOCK, {"key": key})).scalar_one())


@asynccontextmanager
async def advisory_lock(
    engine: AsyncEngine, key: int, *, timeout: float = 0.0, poll_interval: float = 0.1
) -> AsyncIterator[AsyncConnection]:
    """Взять блокировку по ключу. timeout=0 — одна попытка (pg_try_advisory_lock).

    Raises:
        LockNotAcquiredError: блокировка занята дольше timeout.
    """
    # AUTOCOMMIT: каждый запрос — своя транзакция, соединение не держит открытую транзакцию.
    async with engine.connect() as raw_conn:
        conn = await raw_conn.execution_options(isolation_level="AUTOCOMMIT")
        acquired = False
        state_known = True
        try:
            deadline = time.monotonic() + timeout
            while True:
                state_known = False  # запрос мог выполниться, а ответ — потеряться
                acquired = await _try_lock(conn, key)
                state_known = True
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    raise LockNotAcquiredError(key)
                await asyncio.sleep(poll_interval)
            yield conn
        finally:
            if acquired or not state_known:
                await _release(conn, key, state_known=state_known)


async def _release(conn: AsyncConnection, key: int, *, state_known: bool) -> None:
    released = False
    interrupted: BaseException | None = None
    if state_known:
        try:
            released = bool((await conn.execute(_UNLOCK, {"key": key})).scalar_one())
        except Exception:
            log.warning("advisory_unlock_failed", lock_key=key, exc_info=True)
        except BaseException as exc:  # отмена задачи во время снятия блокировки
            interrupted = exc
    if not released:
        # Закрываем физическое соединение, а не возвращаем его в пул с блокировкой.
        log.warning("advisory_lock_connection_invalidated", lock_key=key)
        await conn.invalidate()
    if interrupted is not None:
        raise interrupted
