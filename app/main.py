import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

import sentry_sdk
import structlog
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sqlalchemy import func, select, text

from app.api.routes import router
from app.bot.application import make_bot
from app.bot.posts import sync_outdated
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import APP_ERRORS, HTTP_LATENCY, HTTP_REQUESTS, OPEN_TICKETS
from app.db.models import Ticket, TicketStatus
from app.db.session import SessionLocal, engine
from app.services.maintenance import purge_expired_state
from app.services.reminders import send_due_reminders

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=0.05,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
    )
log = structlog.get_logger()
bot_app = make_bot() if settings.bot_token else None


@asynccontextmanager
async def lifespan(_: FastAPI):
    workers: list[asyncio.Task] = []
    metrics_server = None
    metrics_task = None
    if bot_app:
        await bot_app.initialize()
        await bot_app.start()
        if settings.bot_mode == "polling":
            await bot_app.updater.start_polling()  # type: ignore[union-attr]
        workers = [asyncio.create_task(sync_worker()), asyncio.create_task(reminder_worker())]
    workers.append(asyncio.create_task(maintenance_worker()))
    metrics_server = uvicorn.Server(
        uvicorn.Config(metrics_app, host=settings.host, port=settings.metrics_port, log_level=None)
    )
    metrics_task = asyncio.create_task(metrics_server.serve())
    yield
    for worker in workers:
        worker.cancel()
    for worker in workers:
        with suppress(asyncio.CancelledError):
            await worker
    if metrics_server:
        metrics_server.should_exit = True
    if metrics_task:
        await metrics_task
    if bot_app:
        if settings.bot_mode == "polling":
            await bot_app.updater.stop()  # type: ignore[union-attr]
        await bot_app.stop()
        await bot_app.shutdown()
    await engine.dispose()


app = FastAPI(title="Rent Support Bot", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "miniapp"), name="static")


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    return {"status": "ok"}


metrics_app = FastAPI()


@app.middleware("http")
async def http_metrics(request: Request, call_next):
    import time

    from structlog.contextvars import bind_contextvars, clear_contextvars

    started = time.monotonic()
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    clear_contextvars()
    bind_contextvars(request_id=request_id, component="api")
    sentry_sdk.set_tag("request_id", request_id)
    try:
        response = await call_next(request)
        route = request.scope.get("route")
        route_name = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(request.method, route_name, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route_name).observe(time.monotonic() - started)
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        clear_contextvars()


@metrics_app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    async with SessionLocal() as session:
        counts = dict(
            (status, count)
            for status, count in (
                await session.execute(
                    select(Ticket.status, func.count())
                    .where(
                        Ticket.status.in_(
                            [TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_INFO]
                        )
                    )
                    .group_by(Ticket.status)
                )
            ).all()
        )
    for status in (TicketStatus.NEW, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_INFO):
        OPEN_TICKETS.labels(status.value).set(counts.get(status, 0))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def sync_worker() -> None:
    while True:
        if bot_app:
            try:
                await sync_outdated(bot_app.bot)
            except Exception:
                APP_ERRORS.labels("post_sync").inc()
                log.exception("post_sync_worker_failed")
                sentry_sdk.capture_exception()
        await asyncio.sleep(settings.sync_poll_seconds)


async def reminder_worker() -> None:
    while True:
        if bot_app and settings.developer_chat_id:
            try:
                await send_due_reminders(bot_app.bot, settings.developer_chat_id)
            except Exception:
                APP_ERRORS.labels("reminders").inc()
                log.exception("reminder_worker_failed")
                sentry_sdk.capture_exception()
        await asyncio.sleep(settings.reminder_poll_seconds)


async def maintenance_worker() -> None:
    while True:
        try:
            await purge_expired_state()
        except Exception:
            APP_ERRORS.labels("maintenance").inc()
            log.exception("maintenance_worker_failed")
            sentry_sdk.capture_exception()
        await asyncio.sleep(3600)


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)
) -> dict[str, str]:
    if (
        not settings.telegram_webhook_secret
        or x_telegram_bot_api_secret_token != settings.telegram_webhook_secret
    ):
        raise HTTPException(403, "Invalid webhook secret")
    if bot_app is None:
        raise HTTPException(503, "Bot is not configured")
    from telegram import Update

    update = Update.de_json(await request.json(), bot_app.bot)
    try:
        from app.services.updates import claim_update

        if not await claim_update(update.update_id):
            from app.core.metrics import UPDATES_DUPLICATED

            UPDATES_DUPLICATED.inc()
            return {"status": "duplicate"}
        from app.core.metrics import UPDATES_PROCESSED

        UPDATES_PROCESSED.inc()
        sentry_sdk.set_tag("update_id", update.update_id)
        await asyncio.wait_for(
            bot_app.process_update(update), timeout=settings.webhook_timeout_seconds
        )
    except Exception:
        APP_ERRORS.labels("webhook").inc()
        log.exception("telegram_update_failed", update_id=update.update_id)
        sentry_sdk.capture_exception()
    return {"status": "ok"}


@app.get("/")
async def miniapp() -> FileResponse:
    return FileResponse(Path(__file__).parent / "miniapp" / "index.html")
