"""Webhook-эндпоинт (SPEC «FastAPI»): X-Telegram-Bot-Api-Secret-Token,
синхронная обработка внутри запроса, 200 даже при ошибке обработки."""

from __future__ import annotations

import asyncio
import hmac
import uuid

import sentry_sdk
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from structlog.contextvars import bind_contextvars, clear_contextvars
from telegram import Update

from app.log import get_logger
from app.metrics import errors_total

log = get_logger("bot.webhook")
router = APIRouter()


@router.post("/telegram/webhook", response_model=None)
async def webhook(request: Request) -> JSONResponse | dict[str, bool]:
    settings = request.app.state.settings
    if settings.bot_mode != "webhook":
        return JSONResponse({"error": "webhook disabled"}, status_code=404)
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not token or not hmac.compare_digest(token, settings.webhook_secret_token):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    try:
        payload = await request.json()
        update = Update.de_json(payload, request.app.state.tg_app.bot)
    except Exception:
        return JSONResponse({"error": "invalid update"}, status_code=400)
    clear_contextvars()
    bind_contextvars(
        request_id=uuid.uuid4().hex,
        update_id=getattr(update, "update_id", None),
        telegram_user_id=getattr(getattr(update, "effective_user", None), "id", None),
    )
    try:
        await asyncio.wait_for(
            request.app.state.tg_app.process_update(update),
            timeout=settings.update_timeout_seconds,
        )
    except Exception as exc:
        log.exception("update processing failed")
        errors_total.labels(component="webhook").inc()
        sentry_sdk.capture_exception(exc)
    finally:
        clear_contextvars()
    return {"ok": True}
