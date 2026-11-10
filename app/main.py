import asyncio
import hmac
import logging
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import sentry_sdk
import structlog
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import REGISTRY, Counter, Histogram, start_http_server
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert
from telegram import Update
from telegram.ext import Application

from app.api.routes import router
from app.bot.handlers import register
from app.config import Settings
from app.db.models import ProcessedUpdate, TicketDraft
from app.db.session import create_database
from app.metrics import ERRORS, OpenTicketsCollector
from app.services.posts import sync_pending
from app.services.reminders import claim_reminders

log = logging.getLogger(__name__)
UPDATES = Counter("bot_updates_total", "Telegram updates", ["result"])
HTTP = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["method", "route"])
REMINDERS = Counter("ticket_reminders_total", "Sent reminders")


async def process_update(app, update):
    async with app.state.sessions.begin() as session:
        inserted = await session.scalar(
            insert(ProcessedUpdate)
            .values(update_id=update.update_id)
            .on_conflict_do_nothing()
            .returning(ProcessedUpdate.update_id)
        )
    if inserted is None:
        UPDATES.labels("duplicate").inc()
        return
    UPDATES.labels("processed").inc()
    tokens = structlog.contextvars.bind_contextvars(
        update_id=update.update_id,
        telegram_user_id=update.effective_user.id if update.effective_user else None,
        component="telegram",
    )
    try:
        with sentry_sdk.isolation_scope() as scope:
            scope.set_tag("component", "telegram")
            scope.set_tag("update_id", update.update_id)
            if update.effective_user:
                scope.set_tag("telegram_user_id", update.effective_user.id)
            try:
                async with asyncio.timeout(app.state.settings.webhook_timeout_seconds):
                    await app.state.bot.process_update(update)
            except Exception as exc:
                ERRORS.labels("telegram").inc()
                log.exception("Update processing failed")
                sentry_sdk.capture_exception(exc)
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


async def telegram_error(update, context):
    ERRORS.labels("telegram_handler").inc()
    log.error("Telegram handler failed", exc_info=context.error)
    sentry_sdk.capture_exception(context.error)


async def sync_worker(app):
    while True:
        try:
            await sync_pending(app.state.engine, app.state.bot.bot, app.state.settings.dev_chat_id)
        except Exception as exc:
            ERRORS.labels("post_sync").inc()
            log.exception("Post synchronization worker failed")
            sentry_sdk.capture_exception(exc)
        await asyncio.sleep(app.state.settings.sync_poll_seconds)


async def reminder_worker(app):
    while True:
        try:
            reminders = await claim_reminders(app.state.sessions, app.state.intervals)
            for ticket_id, message_id in reminders:
                try:
                    kwargs = {"reply_to_message_id": message_id} if message_id else {}
                    await app.state.bot.bot.send_message(
                        chat_id=app.state.settings.dev_chat_id,
                        text=f"Напоминание: обращение #{ticket_id} ещё не взято.",
                        **kwargs,
                    )
                    REMINDERS.inc()
                except Exception as exc:
                    ERRORS.labels("reminder").inc()
                    log.exception("Reminder failed")
                    sentry_sdk.capture_exception(exc)
        except Exception as exc:
            ERRORS.labels("reminder_worker").inc()
            log.exception("Reminder worker failed")
            sentry_sdk.capture_exception(exc)
        await asyncio.sleep(app.state.settings.reminder_poll_seconds)


async def cleanup_worker(app):
    while True:
        try:
            async with app.state.sessions.begin() as session:
                await session.execute(
                    delete(ProcessedUpdate).where(
                        ProcessedUpdate.created_at < datetime.now(UTC) - timedelta(hours=48)
                    )
                )
                await session.execute(
                    delete(TicketDraft).where(
                        TicketDraft.updated_at
                        < datetime.now(UTC) - timedelta(hours=app.state.settings.draft_ttl_hours)
                    )
                )
        except Exception as exc:
            ERRORS.labels("cleanup").inc()
            log.exception("Cleanup worker failed")
            sentry_sdk.capture_exception(exc)
        await asyncio.sleep(300)


async def polling(app):
    offset = None
    while True:
        try:
            updates = await app.state.bot.bot.get_updates(offset=offset, timeout=20)
            for update in updates:
                await process_update(app, update)
                offset = update.update_id + 1
        except Exception:
            log.exception("Polling failed")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app):
    settings = Settings()  # type: ignore[call-arg]
    renderer = (
        structlog.processors.JSONRenderer()
        if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.add_log_level,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn, integrations=[FastApiIntegration(), SqlalchemyIntegration()]
        )
    engine, sessions = create_database(settings.database_url)
    bot = Application.builder().token(settings.bot_token).updater(None).build()
    intervals = {
        "critical": settings.reminder_critical_minutes,
        "high": settings.reminder_high_minutes,
        "medium": settings.reminder_medium_minutes,
        "low": settings.reminder_low_minutes,
    }
    bot.bot_data.update(engine=engine, sessions=sessions, settings=settings, intervals=intervals)
    register(bot)
    bot.add_error_handler(telegram_error)
    app.state.settings, app.state.engine, app.state.sessions, app.state.bot, app.state.intervals = (
        settings,
        engine,
        sessions,
        bot,
        intervals,
    )
    await bot.initialize()
    await bot.start()
    collector = OpenTicketsCollector(sessions, asyncio.get_running_loop())
    REGISTRY.register(collector)
    metrics_server, metrics_thread = start_http_server(settings.metrics_port)
    tasks = [
        asyncio.create_task(sync_worker(app)),
        asyncio.create_task(reminder_worker(app)),
        asyncio.create_task(cleanup_worker(app)),
    ]
    if settings.bot_mode == "polling":
        await bot.bot.delete_webhook()
        tasks.append(asyncio.create_task(polling(app)))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        metrics_server.shutdown()
        metrics_server.server_close()
        REGISTRY.unregister(collector)
        await bot.stop()
        await bot.shutdown()
        await engine.dispose()


app = FastAPI(lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def metrics(request: Request, call_next):
    start = perf_counter()
    status = 500
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(request_id=uuid4().hex, component="http")
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", "unmatched")
        HTTP.labels(request.method, template, str(status)).inc()
        LATENCY.labels(request.method, template).observe(perf_counter() - start)
        structlog.contextvars.clear_contextvars()


@app.post("/telegram/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str = Header(default="")):
    if not hmac.compare_digest(
        x_telegram_bot_api_secret_token, request.app.state.settings.webhook_secret
    ):
        raise HTTPException(403, "Forbidden")
    try:
        update = Update.de_json(await request.json(), request.app.state.bot.bot)
        await process_update(request.app, update)
    except Exception as exc:
        log.exception("Webhook processing failed")
        sentry_sdk.capture_exception(exc)
    return {"ok": True}


@app.get("/health/live")
async def live():
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(request: Request):
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(503, "Database unavailable")
    return {"status": "ok"}


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
