import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app import logging_setup
from app.api.errors import register_exception_handlers
from app.api.tickets import router as tickets_router
from app.api.users import router as users_router
from app.api.webhook import router as webhook_router
from app.bot.application import build_application
from app.bot.handlers.common import Deps
from app.config import Settings, settings
from app.db import session as db_session
from app.logging_setup import get_logger
from app.metrics import MetricsMiddleware, create_metrics_app
from app.polling import PtbPollingSource, run_polling
from app.sentry import init_sentry
from app.telegram_gateway import TelegramGateway
from app.workers.cleanup_worker import run_cleanup_worker
from app.workers.post_sync_worker import run_post_sync_worker
from app.workers.reminder_worker import run_reminder_worker

log = get_logger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg: Settings = app.state.settings
    tasks: list[asyncio.Task[None]] = []
    stop = asyncio.Event()
    app.state.stop_event = stop

    # /metrics lives on its own port so business metrics never cross the public ingress.
    metrics_server = uvicorn.Server(
        uvicorn.Config(
            create_metrics_app(db_session.Session),
            host="0.0.0.0",
            port=cfg.metrics_port,
            log_level="warning",
        )
    )
    metrics_task = asyncio.create_task(metrics_server.serve(), name="metrics-server")

    gateway: TelegramGateway | None = None
    application = None
    if app.state.bot_enabled and cfg.bot_token:
        gateway = TelegramGateway(cfg.bot_token)
        await gateway.start()
        application = build_application(
            Deps(gateway=gateway, session_factory=db_session.Session, settings=cfg)
        )
        await application.initialize()
        await application.start()
        app.state.gateway = gateway
        app.state.update_processor = application
        app.state.ptb_application = application

        workers = [run_cleanup_worker(db_session.Session, cfg, stop)]
        if cfg.bot_mode == "polling":
            assert gateway is not None and application is not None
            workers.append(
                run_polling(
                    source=PtbPollingSource(application.bot),
                    processor=application,
                    session_factory=db_session.Session,
                    settings=cfg,
                    stop=stop,
                )
            )
        tasks = [asyncio.create_task(coro, name=f"worker-{i}") for i, coro in enumerate(workers)]
        # Post-sync sweeper keeps dev-chat posts convergent no matter which UI changed the row.
        tasks.append(
            asyncio.create_task(
                run_post_sync_worker(
                    gateway,
                    db_session.Session,
                    cfg,
                    stop,
                    interval_seconds=cfg.post_sync_interval_seconds,
                ),
                name="worker-post-sync",
            )
        )
        tasks.append(
            asyncio.create_task(
                run_reminder_worker(
                    gateway,
                    db_session.Session,
                    cfg,
                    stop,
                    interval_seconds=cfg.reminder_poll_interval_seconds,
                ),
                name="worker-reminders",
            )
        )
    else:
        app.state.update_processor = None

    log.info("startup", component="app", bot_mode=cfg.bot_mode, bot_enabled=app.state.bot_enabled)
    try:
        yield
    finally:
        stop.set()
        metrics_server.should_exit = True
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await metrics_task
        if application is not None:
            await application.shutdown()
        if gateway is not None:
            await gateway.stop()
        await db_session.engine.dispose()
        log.info("shutdown", component="app")


def create_app(cfg: Settings | None = None, *, bot_enabled: bool = False) -> FastAPI:
    cfg = cfg or settings
    logging_setup.configure_logging(cfg.log_level, cfg.log_format)
    init_sentry(cfg)

    app = FastAPI(title="rent-support-bot", lifespan=lifespan)

    app.state.settings = cfg
    app.state.session_factory = db_session.Session
    app.state.gateway = None
    app.state.update_processor = None
    app.state.bot_enabled = bot_enabled
    app.add_middleware(MetricsMiddleware)

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

    app.include_router(webhook_router)
    app.include_router(tickets_router)
    app.include_router(users_router)
    register_exception_handlers(app)

    mini_app_dir = Path(__file__).parent / "static" / "mini_app"
    if mini_app_dir.is_dir():
        app.mount("/miniapp", StaticFiles(directory=mini_app_dir, html=True), name="mini_app")
    return app


# Served by uvicorn in production (compose/prod image); tests create their own app.
app = create_app(bot_enabled=True)
