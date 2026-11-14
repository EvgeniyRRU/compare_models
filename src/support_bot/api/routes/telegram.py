from __future__ import annotations

import asyncio
import hmac

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from telegram import Update

from support_bot.observability.metrics import observe_error, observe_update
from support_bot.observability.sentry import capture_exception
from support_bot.services.updates import try_mark_processed

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["telegram"])


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(received_secret, settings.webhook_secret_value):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid webhook secret")

    try:
        payload = await request.json()
    except ValueError:
        return {"ok": True}
    if not isinstance(payload, dict):
        return {"ok": True}
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        return {"ok": True}

    try:
        is_new = await try_mark_processed(request.app.state.session_factory, update_id)
    except Exception as exc:
        logger.exception("update dedup failed", component="webhook")
        observe_error("webhook")
        capture_exception(exc, component="webhook")
        return {"ok": True}
    observe_update(duplicate=not is_new)
    if not is_new:
        logger.info("duplicate update skipped", component="webhook", update_id=update_id)
        return {"ok": True}

    application = request.app.state.bot_application
    try:
        update = Update.de_json(payload, application.bot)
    except Exception as exc:
        logger.exception("cannot parse telegram update", component="webhook", update_id=update_id)
        observe_error("webhook")
        capture_exception(exc, component="webhook", update_id=update_id)
        return {"ok": True}

    context: dict[str, object] = {"update_id": update_id, "component": "webhook"}
    if update.effective_user is not None:
        context["telegram_user_id"] = update.effective_user.id
    with structlog.contextvars.bound_contextvars(**context):
        try:
            await asyncio.wait_for(
                application.process_update(update),
                timeout=settings.webhook_timeout_seconds,
            )
        except TimeoutError as exc:
            logger.exception("update processing timed out", component="webhook")
            observe_error("webhook")
            capture_exception(exc, component="webhook", update_id=update_id)
        except Exception as exc:
            logger.exception("update processing failed", component="webhook")
            observe_error("webhook")
            capture_exception(exc, component="webhook", update_id=update_id)
    return {"ok": True}
