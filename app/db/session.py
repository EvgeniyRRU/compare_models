from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import settings


def make_engine(url: str, **kwargs: object) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True, **kwargs)


# Module-level engine/session factory: created at import from settings, disposed on shutdown.
engine = make_engine(settings.database_url)
Session = async_sessionmaker(engine, expire_on_commit=False)
