from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


class DB:
    """Два движка:
    - engine: основной пул для бизнес-транзакций;
    - lock_engine: NullPool — выделенные ФИЗИЧЕСКИЕ соединения для session-level
      advisory lock (после close() соединение закрывается, а не возвращается в пул;
      блокировка живёт до явного pg_advisory_unlock или конца сессии).
    """

    def __init__(self, engine: AsyncEngine, lock_engine: AsyncEngine):
        self.engine = engine
        self.lock_engine = lock_engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def dispose(self) -> None:
        await self.engine.dispose()
        await self.lock_engine.dispose()


def make_db(database_url: str) -> DB:
    return DB(
        engine=create_async_engine(database_url, pool_pre_ping=True),
        lock_engine=create_async_engine(database_url, poolclass=NullPool),
    )
