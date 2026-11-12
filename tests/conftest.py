import os

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Base
from app.db.session import engine as app_engine


@pytest_asyncio.fixture(autouse=True)
async def dispose_app_engine_between_loops():
    yield
    await app_engine.dispose()


@pytest_asyncio.fixture
async def db_session():
    url = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db:5432/support"
    )
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        transaction = await connection.begin_nested()
        factory = async_sessionmaker(bind=connection, expire_on_commit=False)
        async with factory() as session:
            yield session
            await session.close()
        await transaction.rollback()
    await engine.dispose()
