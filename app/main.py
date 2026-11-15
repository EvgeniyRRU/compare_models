from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from sqlalchemy import text

from app import logging_setup
from app.config import Settings, settings
from app.db import session as db_session
from app.logging_setup import get_logger
from app.sentry import init_sentry

log = get_logger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("startup", component="app", bot_mode=settings.bot_mode)
    try:
        yield
    finally:
        await db_session.engine.dispose()
        log.info("shutdown", component="app")


def create_app(cfg: Settings | None = None) -> FastAPI:
    cfg = cfg or settings
    logging_setup.configure_logging(cfg.log_level, cfg.log_format)
    init_sentry(cfg)

    app = FastAPI(title="rent-support-bot", lifespan=lifespan)

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(response: Response) -> dict[str, str]:
        try:
            async with db_session.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            log.warning("readiness_failed", error=str(exc), component="health")
            response.status_code = 503
            return {"status": "db_unavailable"}
        return {"status": "ok"}

    return app


app = create_app()
