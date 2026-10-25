"""FastAPI-приложение. Запуск: `uvicorn support_bot.main:create_app --factory`.

Жизненный цикл PTB (Application без Updater), фоновых задач и сервера метрик привязан
к lifespan FastAPI. В каждом pod работает ровно один процесс uvicorn.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from prometheus_client import REGISTRY, start_http_server

from support_bot.api.errors import install_error_handlers
from support_bot.api.routes import router as api_router
from support_bot.api.system import router as system_router
from support_bot.config import Settings, get_settings
from support_bot.container import Container, build_container
from support_bot.db.engine import create_engine
from support_bot.logging import configure_logging
from support_bot.monitoring.collector import OpenTicketsCollector
from support_bot.monitoring.middleware import ObservabilityMiddleware
from support_bot.monitoring.sentry import init_sentry
from support_bot.telegram.application import build_application, configure_menu_button, run_polling
from support_bot.telegram.handlers import BotHandlers
from support_bot.telegram.pipeline import UpdatePipeline
from support_bot.telegram.publisher import TelegramPublisher
from support_bot.workers.loops import start_workers, stop_tasks

log = structlog.get_logger(__name__)

WEBAPP_DIR = Path(__file__).parent / "webapp"


def _check_settings(settings: Settings) -> None:
    if settings.bot_mode == "webhook" and settings.webhook_secret is None:
        raise RuntimeError("WEBHOOK_SECRET обязателен в режиме BOT_MODE=webhook")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    _check_settings(settings)
    engine = create_engine(settings)
    bot_app = build_application(settings)
    publisher = TelegramPublisher(
        bot_app.bot,
        settings.dev_chat_id,
        max_attempts=settings.telegram_max_attempts,
        base_delay=settings.telegram_backoff_base_seconds,
        max_delay=settings.telegram_backoff_max_seconds,
    )
    container = build_container(settings, engine, publisher)
    BotHandlers(container).register(bot_app)
    pipeline = UpdatePipeline(
        container.dedup, bot_app.process_update, timeout=settings.update_timeout_seconds
    )
    app.state.container = container
    app.state.bot_app = bot_app
    app.state.pipeline = pipeline

    collector = OpenTicketsCollector(container.session_factory, asyncio.get_running_loop())
    REGISTRY.register(collector)
    metrics_server, _ = start_http_server(settings.metrics_port)

    await bot_app.initialize()
    await bot_app.start()
    await configure_menu_button(bot_app, settings)

    tasks = start_workers(container)
    if settings.bot_mode == "polling":
        tasks.append(asyncio.create_task(run_polling(bot_app, pipeline), name="polling"))
    log.info("app_started", bot_mode=settings.bot_mode, metrics_port=settings.metrics_port)
    try:
        yield
    finally:
        await stop_tasks(tasks)
        with suppress(Exception):
            await bot_app.stop()
        with suppress(Exception):
            await bot_app.shutdown()
        metrics_server.shutdown()
        REGISTRY.unregister(collector)
        await engine.dispose()
        log.info("app_stopped")


def build_app(settings: Settings, *, container: Container | None = None, **state: Any) -> FastAPI:
    """Собрать FastAPI. С готовым container lifespan не запускается (для тестов)."""
    app = FastAPI(
        title="Support duty bot",
        lifespan=lifespan if container is None else None,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    if container is not None:
        app.state.container = container
    for key, value in state.items():
        setattr(app.state, key, value)
    app.add_middleware(ObservabilityMiddleware)
    install_error_handlers(app)
    app.include_router(system_router)
    app.include_router(api_router)
    app.mount("/app", StaticFiles(directory=WEBAPP_DIR, html=True), name="webapp")
    return app


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    init_sentry(settings)
    return build_app(settings)
