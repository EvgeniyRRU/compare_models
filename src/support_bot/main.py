import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import sentry_sdk
from fastapi import FastAPI
from prometheus_client import REGISTRY, start_http_server
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telegram import MenuButtonWebApp, WebAppInfo

from support_bot.bot.app import build_application
from support_bot.config import Settings, get_settings
from support_bot.logging import configure_logging
from support_bot.metrics import OpenTicketsCollector
from support_bot.runtime import Runtime
from support_bot.web.app import create_app
from support_bot.workers import poll_updates, start_workers

OPEN_STATUSES = ("new", "in_progress", "waiting_info")


def init_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        traces_sample_rate=0.0,
    )


async def _open_counts(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with sessions() as session:
        rows = await session.execute(
            text("SELECT status, count(*) FROM tickets WHERE status = ANY(:open) GROUP BY status"),
            {"open": list(OPEN_STATUSES)},
        )
        counts = {status: 0 for status in OPEN_STATUSES}
        counts.update({status: int(count) for status, count in rows.all()})
    return counts


def _register_open_tickets_gauge(
    loop: asyncio.AbstractEventLoop, sessions: async_sessionmaker[AsyncSession]
) -> OpenTicketsCollector:
    def fetch() -> dict[str, int]:
        # Scrapes run in the metrics thread; the query runs on the app's event loop.
        future = asyncio.run_coroutine_threadsafe(_open_counts(sessions), loop)
        return future.result(timeout=5)

    collector = OpenTicketsCollector(fetch)
    REGISTRY.register(collector)
    return collector


async def _configure_telegram(application: Any, settings: Settings) -> None:
    bot = application.bot
    if settings.bot_mode == "webhook":
        assert settings.webhook_url and settings.webhook_secret_token
        await bot.set_webhook(
            url=settings.webhook_url,
            secret_token=settings.webhook_secret_token.get_secret_value(),
        )
    else:
        await bot.delete_webhook()
    if settings.mini_app_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Обращения", web_app=WebAppInfo(settings.mini_app_url)
            )
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    init_sentry(settings)

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    application = build_application(settings, engine, sessions)
    await application.initialize()
    await application.start()
    runtime: Runtime = application.bot_data["runtime"]

    app.state.settings = settings
    app.state.runtime = runtime
    app.state.application = application

    start_http_server(settings.metrics_port)
    gauge = _register_open_tickets_gauge(asyncio.get_running_loop(), sessions)
    await _configure_telegram(application, settings)

    tasks = start_workers(runtime)
    if settings.bot_mode == "polling":
        tasks.append(asyncio.create_task(poll_updates(application, runtime)))
    try:
        yield
    finally:
        REGISTRY.unregister(gauge)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await application.stop()
        await application.shutdown()
        await engine.dispose()


app = create_app(lifespan=lifespan)
