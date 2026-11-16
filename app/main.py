from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import log
from app.api.health import router as health_router
from app.config import Settings, get_settings
from app.db import make_db


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.configure_logging(settings.log_level, settings.log_format)
    settings.validate_modes()
    app.state.db = make_db(settings.database_url)
    yield
    await app.state.db.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="rent-support-bot", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.include_router(health_router)
    return app


app = create_app()
