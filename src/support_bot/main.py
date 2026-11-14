from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from support_bot.api.router import api_router
from support_bot.bot.application import attach_services, build_application
from support_bot.bot.common import BotServices
from support_bot.bot.polling import polling_loop
from support_bot.config import Settings, get_settings
from support_bot.db.session import SessionFactory, create_engine, create_session_factory
from support_bot.domain.reminders import ReminderPolicy
from support_bot.integrations.telegram_post import TelegramPostPublisher
from support_bot.logging import configure_logging
from support_bot.observability.context import RequestContextMiddleware
from support_bot.observability.metrics import (
    METRICS_CONTENT_TYPE,
    PrometheusMiddleware,
    refresh_open_tickets,
    render_metrics,
)
from support_bot.observability.sentry import init_sentry
from support_bot.services.post_sync import PostSyncService
from support_bot.workers import PeriodicRunner
from support_bot.workers.cleanup import cleanup_tick
from support_bot.workers.post_sync import post_sync_tick
from support_bot.workers.reminders import reminders_tick

logger = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"


async def _metrics_endpoint(request: Request) -> Response:
    try:
        async with request.app.state.session_factory() as session:
            await refresh_open_tickets(session)
    except Exception:
        logger.exception("metrics gauge refresh failed", component="metrics")
    return Response(render_metrics(), media_type=METRICS_CONTENT_TYPE)


def create_metrics_app(session_factory: SessionFactory) -> Starlette:
    app = Starlette(routes=[Route("/metrics", _metrics_endpoint)])
    app.state.session_factory = session_factory
    return app


class _NoSignalServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        return None


class MetricsServer:
    def __init__(self, app: Starlette, port: int) -> None:
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_config=None,
            access_log=False,
            lifespan="off",
        )
        self._server = _NoSignalServer(config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve())

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings)
    init_sentry(settings)
    settings.validate_runtime()

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.reminder_policy = ReminderPolicy.from_settings(settings)

    bot_application = build_application(settings)
    publisher = TelegramPostPublisher(bot_application.bot)
    post_sync = PostSyncService(
        session_factory=session_factory,
        publisher=publisher,
        chat_id=settings.dev_chat_id,
        database_dsn=settings.database_dsn,
        lock_timeout=settings.post_sync_lock_timeout_seconds,
    )
    services = BotServices(
        settings=settings,
        session_factory=session_factory,
        post_sync=post_sync,
        policy=app.state.reminder_policy,
    )
    attach_services(bot_application, services)
    app.state.bot_application = bot_application
    app.state.post_sync = post_sync

    await bot_application.initialize()
    await bot_application.start()

    stop_event = asyncio.Event()
    polling_task: asyncio.Task[None] | None = None
    if settings.bot_mode == "polling":
        polling_task = asyncio.create_task(polling_loop(bot_application, services, stop_event))

    runner = PeriodicRunner()
    if settings.workers_enabled:
        runner.start(
            "post_sync",
            settings.post_sync_poll_seconds,
            lambda: post_sync_tick(
                session_factory, post_sync, batch_size=settings.post_sync_batch_size
            ),
        )
        runner.start(
            "reminders",
            settings.reminder_poll_seconds,
            lambda: reminders_tick(
                session_factory,
                publisher,
                services.policy,
                chat_id=settings.dev_chat_id,
                batch_size=settings.reminder_batch_size,
            ),
        )
        runner.start(
            "cleanup",
            settings.cleanup_poll_seconds,
            lambda: cleanup_tick(
                session_factory,
                draft_ttl_hours=settings.draft_ttl_hours,
                updates_retention_hours=settings.updates_retention_hours,
            ),
        )

    metrics_server: MetricsServer | None = None
    if settings.metrics_enabled:
        metrics_server = MetricsServer(create_metrics_app(session_factory), settings.metrics_port)
        await metrics_server.start()

    logger.info(
        "application started",
        component="lifespan",
        bot_mode=settings.bot_mode,
        workers=settings.workers_enabled,
    )
    try:
        yield
    finally:
        stop_event.set()
        if polling_task is not None:
            polling_task.cancel()
            await asyncio.gather(polling_task, return_exceptions=True)
        await runner.stop()
        if metrics_server is not None:
            await metrics_server.stop()
        await bot_application.stop()
        await bot_application.shutdown()
        await engine.dispose()
        logger.info("application stopped", component="lifespan")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Support Bot", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(PrometheusMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(api_router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/app", include_in_schema=False)
    async def mini_app() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
