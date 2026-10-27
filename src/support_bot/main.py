"""FastAPI factory and owned process resources. Migrations are a separate command."""

import asyncio
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import start_http_server
from structlog.contextvars import bound_contextvars
from telegram import Update
from telegram.ext import Application, ExtBot
from telegram.request import HTTPXRequest

from support_bot.config import Settings
from support_bot.db.base import Database
from support_bot.domain.errors import BusinessError
from support_bot.observability.logging import configure_logging
from support_bot.observability.metrics import HTTPMetricsMiddleware, OpenTicketsCollector, metrics
from support_bot.observability.sentry import configure_sentry
from support_bot.runtime import UpdateProcessor, polling_loop
from support_bot.services.drafts import DraftService
from support_bot.services.posts import PostService
from support_bot.services.reminders import ReminderService
from support_bot.services.tickets import TicketService
from support_bot.services.updates import UpdateService
from support_bot.services.users import UserService
from support_bot.telegram.transport import TelegramTransport, Transport
from support_bot.workers import start_workers, stop_workers


def create_app(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    application: Application | None = None,
    transport: Transport | None = None,
    enable_workers: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    db = database or Database(settings)
    users = UserService(db.sessions, settings)
    tickets = TicketService(db.sessions, settings)
    drafts = DraftService(db.sessions, settings)
    bot = (
        application.bot
        if application is not None
        else ExtBot(
            settings.bot_token.get_secret_value(),
            request=HTTPXRequest(
                connection_pool_size=32, read_timeout=settings.telegram_timeout_seconds
            ),
            get_updates_request=HTTPXRequest(read_timeout=25),
        )
    )
    telegram_transport = transport or TelegramTransport(bot, settings)
    posts = PostService(db.engine, db.sessions, settings, telegram_transport)
    if application is None:
        from support_bot.telegram.application import build_application

        application = build_application(settings, users, tickets, drafts, posts, bot=bot)
    updates = UpdateService(db.sessions, settings.worker_batch_size)
    processor = UpdateProcessor(application, updates, settings)
    reminders = ReminderService(db.sessions, settings, telegram_transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        configure_logging(
            settings.log_level,
            settings.log_format,
            (settings.bot_token.get_secret_value(), settings.webhook_secret.get_secret_value()),
        )
        sentry_guard = configure_sentry(settings)
        tasks: list[asyncio.Task[None]] = []
        collector: OpenTicketsCollector | None = None
        metrics_server: Any = None
        metrics_thread: Any = None
        initialized = started = False
        try:
            await application.initialize()
            initialized = True
            await application.start()
            started = True
            if enable_workers:
                collector = OpenTicketsCollector(db.sessions, asyncio.get_running_loop())
                metrics.registry.register(collector)
                metrics_server, metrics_thread = start_http_server(
                    settings.metrics_port,
                    addr=settings.metrics_host,
                    registry=metrics.registry,
                )
                tasks = start_workers(db, posts, reminders, updates, settings)
                if settings.bot_mode == "polling":
                    tasks.append(
                        asyncio.create_task(polling_loop(application, processor), name="polling")
                    )
            structlog.get_logger().info(
                "application_started", component="lifecycle", mode=settings.bot_mode
            )
            yield
        finally:
            await stop_workers(tasks)
            if metrics_server is not None:
                await asyncio.to_thread(metrics_server.shutdown)
                metrics_server.server_close()
                await asyncio.to_thread(metrics_thread.join, 5)
            if collector is not None:
                metrics.registry.unregister(collector)
            if started:
                await application.stop()
            if initialized:
                await application.shutdown()
            if database is None:
                await db.engine.dispose()
            if sentry_guard is not None:
                sentry_guard.__exit__(None, None, None)

    app = FastAPI(title="Учёт обращений", lifespan=lifespan, docs_url=None, redoc_url=None)
    for name, value in {
        "settings": settings,
        "db": db,
        "users": users,
        "tickets": tickets,
        "drafts": drafts,
        "posts": posts,
        "application": application,
        "processor": processor,
        "metrics": metrics,
    }.items():
        setattr(app.state, name, value)
    app.add_middleware(HTTPMetricsMiddleware, metrics=metrics)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        supplied_id = request.headers.get("x-request-id", "")
        request_id = (
            supplied_id if supplied_id.isascii() and 0 < len(supplied_id) <= 128 else uuid4().hex
        )
        with bound_contextvars(request_id=request_id, component="http"):
            try:
                response = await call_next(request)
            except Exception as error:
                from support_bot.observability.sentry import capture_error

                capture_error(error, component="http")
                raise
            response.headers["x-request-id"] = request_id
            return response

    @app.exception_handler(BusinessError)
    async def business_error(request: Request, error: BusinessError) -> JSONResponse:
        status = {
            "forbidden": 403,
            "not_found": 404,
            "conflict": 409,
            "stale_draft": 409,
            "active_tickets": 409,
            "post_restore_failed": 503,
        }.get(error.code, 400)
        return JSONResponse(
            status_code=status,
            content={"code": error.code, "message": error.message, "ticket_ids": error.ticket_ids},
        )

    @app.post("/telegram/webhook")
    async def webhook(request: Request) -> dict[str, bool]:
        if settings.bot_mode != "webhook":
            raise HTTPException(404)
        supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(
            supplied.encode(), settings.webhook_secret.get_secret_value().encode()
        ):
            raise HTTPException(403, "Неверный секрет webhook")
        try:
            data = await request.json()
            if not isinstance(data, dict) or type(data.get("update_id")) is not int:
                raise ValueError("Invalid update_id")
            update = Update.de_json(data, application.bot)
        except (ValueError, TypeError, KeyError) as error:
            raise HTTPException(400, "Некорректный update") from error
        await processor.process(update)
        return {"ok": True}

    from support_bot.api.health import router as health_router
    from support_bot.api.tickets import router as tickets_router
    from support_bot.api.users import router as users_router

    app.include_router(health_router)
    app.include_router(tickets_router)
    app.include_router(users_router)
    static_path = Path(__file__).with_name("static")
    app.mount("/static", StaticFiles(directory=static_path), name="static")

    @app.get("/", include_in_schema=False)
    async def mini_app() -> FileResponse:
        return FileResponse(static_path / "index.html", headers={"Cache-Control": "no-cache"})

    return app
