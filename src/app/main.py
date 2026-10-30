import asyncio
import contextlib
import hmac
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.staticfiles import StaticFiles
from telegram import MenuButtonWebApp, WebAppInfo
from telegram.ext import Application

from app import metrics
from app.api.routes import router as api_router
from app.bot import handlers
from app.bot.context import AppContext
from app.bot.runner import process_raw_update
from app.bot.tg_publisher import TelegramPublisher
from app.config import BotMode, Settings, get_settings
from app.db.session import create_engine, create_sessionmaker
from app.logging_setup import bind, clear, setup_logging
from app.sentry_setup import setup_sentry
from app.services.drafts import DraftService
from app.services.post_sync import PostSyncer
from app.services.tickets import TicketService
from app.workers.loops import start_workers

log = structlog.get_logger(component="app")
STATIC_DIR = Path(__file__).parent / "web" / "static"
ALLOWED_UPDATES = ["message", "callback_query"]


def _metrics_app(ctx_holder: dict[str, AppContext]) -> Starlette:
    async def endpoint(request: Request) -> Response:
        # gauge открытых обращений вычисляется запросом к БД при скрейпе
        await metrics.open_tickets.refresh(ctx_holder["ctx"].sm)
        return Response(metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return Starlette(routes=[Route("/metrics", endpoint)])


async def _poll(application: Application, ctx: AppContext) -> None:  # type: ignore[type-arg]
    offset: int | None = None
    while True:
        try:
            updates = await application.bot.get_updates(
                offset=offset, timeout=30, allowed_updates=ALLOWED_UPDATES
            )
            for u in updates:
                offset = u.update_id + 1
                await process_raw_update(application, ctx, u.to_dict())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("polling_failed")
            await asyncio.sleep(3)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_format)
    setup_sentry(settings.sentry_dsn, settings.environment)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings.database_url)
        sm = create_sessionmaker(engine)
        application = Application.builder().token(settings.bot_token).updater(None).build()
        publisher = TelegramPublisher(application.bot)
        tickets = TicketService(settings.reminder_intervals)
        ctx = AppContext(
            settings=settings,
            engine=engine,
            sm=sm,
            tickets=tickets,
            drafts=DraftService(tickets, timedelta(hours=settings.draft_ttl_hours)),
            syncer=PostSyncer(
                engine, sm, publisher, settings.dev_chat_id, settings.advisory_lock_timeout_seconds
            ),
            publisher=publisher,
        )
        application.bot_data["ctx"] = ctx
        handlers.register(application)
        app.state.ctx = ctx
        app.state.application = application
        await application.initialize()

        tasks: list[asyncio.Task[Any]] = start_workers(ctx)
        if settings.bot_mode == BotMode.WEBHOOK:
            if settings.webhook_url:
                await application.bot.set_webhook(
                    settings.webhook_url,
                    secret_token=settings.webhook_secret or None,
                    allowed_updates=ALLOWED_UPDATES,
                )
        else:
            await application.bot.delete_webhook()
            tasks.append(asyncio.create_task(_poll(application, ctx)))
        if settings.webapp_url:
            try:
                await application.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp("Обращения", WebAppInfo(settings.webapp_url))
                )
            except Exception:
                log.exception("menu_button_failed")

        holder = {"ctx": ctx}
        metrics_server = uvicorn.Server(
            uvicorn.Config(
                _metrics_app(holder), host="0.0.0.0", port=settings.metrics_port, log_config=None
            )
        )
        # _serve(): без перехвата сигналов, чтобы не подменить обработчики основного сервера
        tasks.append(asyncio.create_task(metrics_server._serve()))
        log.info("started", mode=settings.bot_mode.value)
        try:
            yield
        finally:
            metrics_server.should_exit = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await application.shutdown()
            await engine.dispose()

    app = FastAPI(title="Support bot", lifespan=lifespan)

    @app.middleware("http")
    async def http_metrics(request: Request, call_next):  # type: ignore[no-untyped-def]
        clear()
        bind(request_id=request.headers.get("x-request-id") or uuid.uuid4().hex, component="http")
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", "unmatched")  # шаблон пути, не фактический URL
            metrics.HTTP_REQUESTS.labels(request.method, template, str(status)).inc()
            metrics.HTTP_LATENCY.labels(request.method, template).observe(
                time.perf_counter() - start
            )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request) -> dict[str, str]:
        try:
            async with request.app.state.ctx.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as e:
            raise HTTPException(503, "db unavailable") from e
        return {"status": "ok"}

    @app.post("/telegram/webhook")
    async def webhook(request: Request) -> dict[str, bool]:
        secret = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not settings.webhook_secret or not hmac.compare_digest(secret, settings.webhook_secret):
            raise HTTPException(403, "forbidden")
        try:
            data = await request.json()
        except ValueError:
            return {"ok": True}
        # любая ошибка обработки логируется внутри, ответ всегда 200
        await process_raw_update(request.app.state.application, request.app.state.ctx, data)
        return {"ok": True}

    app.include_router(api_router)
    app.mount("/app", StaticFiles(directory=STATIC_DIR, html=True), name="miniapp")
    return app
