import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import log
from app.api.health import router as health_router
from app.api.tickets import router as api_tickets_router
from app.api.users import router as api_users_router
from app.bot.application import build_application
from app.bot.webhook import router as webhook_router
from app.config import Settings, get_settings
from app.db import make_db
from app.workers import (
    run_draft_ttl_loop,
    run_post_sync_loop,
    run_processed_updates_cleanup,
    run_reminder_loop,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    log.configure_logging(settings.log_level, settings.log_format)
    settings.validate_modes()
    app.state.db = make_db(settings.database_url)

    tg_app = build_application(settings, app.state.db)
    await tg_app.initialize()
    await tg_app.start()
    app.state.tg_app = tg_app

    if settings.bot_mode == "polling":
        from telegram.ext import Updater

        updater = Updater(tg_app.bot, tg_app.update_queue)
        await updater.initialize()
        await updater.start_polling()
        app.state.updater = updater

    db = app.state.db
    workers = [
        asyncio.create_task(run_post_sync_loop(settings, db, tg_app.bot), name="post_sync"),
        asyncio.create_task(run_draft_ttl_loop(settings, db), name="draft_ttl"),
        asyncio.create_task(run_processed_updates_cleanup(db), name="dedup_cleanup"),
        asyncio.create_task(run_reminder_loop(settings, db, tg_app.bot), name="reminders"),
    ]
    try:
        yield
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if settings.bot_mode == "polling":
            await app.state.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        await db.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="rent-support-bot", lifespan=lifespan)
    app.state.settings = settings or get_settings()
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.include_router(api_tickets_router, prefix="/api")
    app.include_router(api_users_router, prefix="/api")
    return app


app = create_app()
