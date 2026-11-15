"""FastAPI application factory and health endpoints."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response, status
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from telegram import Update
from telegram.ext import Application

from app.api.routes import api_router
from app.background import (
    run_draft_cleanup_loop,
    run_post_sync_loop,
    run_processed_updates_cleanup_loop,
    run_reminder_loop,
)
from app.bot.application import build_application
from app.bot.post_client import BotPostClient
from app.config import settings
from app.db.session import engine as default_engine
from app.db.session import make_session_factory
from app.logging import bind_contextvars, clear_contextvars, setup_logging
from app.monitoring import init_sentry
from app.webhook import router as webhook_router

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    engine: AsyncEngine | None = None,
    *,
    application: Application[Any, Any, Any, Any, Any, Any] | None = None,
) -> FastAPI:
    db_engine = engine if engine is not None else default_engine

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        application = (
            app.state.application if app.state.application is not None else build_application()
        )
        app.state.application = application
        session_factory = make_session_factory(db_engine)

        tasks: list[asyncio.Task[Any]] = []
        polling = settings.bot_mode != "webhook"

        try:
            setup_logging()
            init_sentry()

            if polling:
                # ``run_polling``/``run_webhook`` drive the event loop themselves and
                # therefore cannot be awaited inside FastAPI's async lifespan, so run the
                # same lifecycle manually: initialize -> start -> start_polling.
                # NOTE: this skips PTB's ``post_init``/``post_shutdown`` callbacks.
                # build_application() registers none today, but Stage 8 (Sentry/handlers)
                # must not rely on ``post_init`` for setup — wire it explicitly instead.
                await application.initialize()
                await application.start()
                updater = application.updater
                if updater is None:  # defensive: build_application() always creates one.
                    raise RuntimeError("polling mode requires an Updater")
                await updater.start_polling(allowed_updates=Update.ALL_TYPES)
            else:
                await application.initialize()
                await application.bot.set_webhook(
                    url=settings.webhook_url, secret_token=settings.webhook_secret or None
                )

            client = BotPostClient(application.bot, settings.dev_chat_id)
            app.state.post_client = client

            async def send_reminder(text: str, reply_to_message_id: int | None) -> None:
                await application.bot.send_message(
                    chat_id=settings.dev_chat_id, text=text, reply_to_message_id=reply_to_message_id
                )

            tasks.extend(
                [
                    asyncio.create_task(
                        run_post_sync_loop(
                            engine=db_engine,
                            session_factory=session_factory,
                            client=client,
                            interval=settings.post_sync_poll_interval,
                        )
                    ),
                    asyncio.create_task(
                        run_reminder_loop(
                            session_factory=session_factory,
                            send_reminder=send_reminder,
                            interval=settings.reminder_poll_interval,
                        )
                    ),
                    asyncio.create_task(
                        run_draft_cleanup_loop(
                            session_factory=session_factory,
                            interval=settings.cleanup_poll_interval,
                        )
                    ),
                    asyncio.create_task(
                        run_processed_updates_cleanup_loop(
                            session_factory=session_factory,
                            interval=settings.cleanup_poll_interval,
                        )
                    ),
                ]
            )

            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if polling:
                try:
                    updater = application.updater
                    if updater is not None and updater.running:
                        await updater.stop()
                    if application.running:
                        await application.stop()
                except Exception:
                    logger.exception("application stop failed")
            try:
                await application.shutdown()
            except Exception:
                logger.exception("application shutdown failed")
            await db_engine.dispose()

    app = FastAPI(title="Rent Support Bot", lifespan=lifespan)
    # The real PTB ``Application`` is built/initialized by the lifespan in a later
    # stage; here it may be injected (tests) or left for the lifespan to create.
    app.state.application = application

    app.include_router(webhook_router)
    app.include_router(api_router)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        bind_contextvars(request_id=request_id, component="http")
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            clear_contextvars()

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

    # Mounted last so the explicit /api and /health routes match first.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()
