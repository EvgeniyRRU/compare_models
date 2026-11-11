import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from telegram import Update

from support_bot.config import Settings
from support_bot.domain.errors import (
    AlreadyTaken,
    DomainError,
    Forbidden,
    InvalidTransition,
    NotFound,
    RoleChangeBlocked,
    StaleTicket,
    ValidationFailed,
)
from support_bot.logging import tagged
from support_bot.metrics import HTTP_LATENCY, HTTP_REQUESTS
from support_bot.pipeline import process_update_once
from support_bot.runtime import Runtime
from support_bot.web import api

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]
log = structlog.get_logger()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    NotFound: 404,
    Forbidden: 403,
    StaleTicket: 409,
    AlreadyTaken: 409,
    InvalidTransition: 409,
    ValidationFailed: 422,
}


def create_app(lifespan: Lifespan | None = None) -> FastAPI:
    """Build the HTTP app. Dependencies are attached on `app.state` by the lifespan."""
    app = FastAPI(
        title="support-bot",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.include_router(api.router)
    app.include_router(_system_router())
    app.mount("/app", StaticFiles(directory=STATIC_DIR, html=True), name="mini-app")

    @app.middleware("http")
    async def metrics_middleware(request: Request, call_next: Any) -> Response:
        started = time.perf_counter()
        with tagged(request_id=uuid.uuid4().hex):
            response: Response = await call_next(request)
        route = request.scope.get("route")
        template = route.path if route is not None else "unmatched"
        HTTP_LATENCY.labels(method=request.method, route=template).observe(
            time.perf_counter() - started
        )
        HTTP_REQUESTS.labels(
            method=request.method, route=template, status=str(response.status_code)
        ).inc()
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        status = _STATUS_BY_ERROR.get(type(exc), 409)
        if isinstance(exc, RoleChangeBlocked):
            return JSONResponse(
                status_code=409,
                content={"detail": str(exc), "ticket_ids": exc.ticket_ids},
            )
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    return app


def _system_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready")
    async def ready(request: Request) -> dict[str, str]:
        runtime: Runtime = request.app.state.runtime
        try:
            async with runtime.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        return {"status": "ok"}

    @router.post("/telegram/webhook")
    async def webhook(request: Request) -> Response:
        settings: Settings = request.app.state.settings
        expected = settings.webhook_secret_token
        if expected is None or request.headers.get(SECRET_HEADER) != expected.get_secret_value():
            raise HTTPException(status_code=403, detail="bad secret token")
        try:
            payload = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc

        runtime: Runtime = request.app.state.runtime
        application = request.app.state.application
        try:
            update = Update.de_json(payload, application.bot)
            if update is not None:
                await process_update_once(application, runtime.sessions, settings, update)
        except Exception:
            # Logged and answered with 200 so Telegram does not resend a failing update.
            log.exception("webhook processing failed", component="webhook")
        return JSONResponse({"ok": True})

    return router
