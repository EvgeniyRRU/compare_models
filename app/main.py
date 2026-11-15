"""FastAPI application factory and health endpoints."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from telegram.ext import Application

from app.db.session import engine as default_engine
from app.webhook import router as webhook_router


def create_app(
    engine: AsyncEngine | None = None,
    *,
    application: Application[Any, Any, Any, Any, Any, Any] | None = None,
) -> FastAPI:
    db_engine = engine if engine is not None else default_engine

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Placeholder: the bot lifecycle and background tasks are wired up in later stages.
        yield
        await db_engine.dispose()

    app = FastAPI(title="Rent Support Bot", lifespan=lifespan)
    # The real PTB ``Application`` is built/initialized by the lifespan in a later
    # stage; here it may be injected (tests) or left for the lifespan to create.
    app.state.application = application

    app.include_router(webhook_router)

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready(response: Response) -> dict[str, str]:
        try:
            async with db_engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable"}
        return {"status": "ok"}

    return app


app = create_app()
