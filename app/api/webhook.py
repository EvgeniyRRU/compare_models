"""Telegram webhook endpoint.

Auth: constant-time compare of X-Telegram-Bot-Api-Secret-Token when the secret is
configured (empty secret = no check, dev only). Processing happens synchronously
inside the request with a timeout; every failure mode still answers 200 so Telegram
does not hammer a known-bad update.
"""

import secrets as hmac_cmp

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from telegram import Update

from app.bot.dispatch import dispatch_update
from app.logging_setup import get_logger

log = get_logger("api.webhook")

router = APIRouter()

HEADER = "X-Telegram-Bot-Api-Secret-Token"


def _dropped(reason: str) -> JSONResponse:
    return JSONResponse(status_code=200, content={"ok": True, "dropped": reason})


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    state = request.app.state
    cfg = state.settings
    secret: str = cfg.webhook_secret_token
    if secret:
        header = request.headers.get(HEADER, "")
        if not hmac_cmp.compare_digest(header, secret):
            return JSONResponse(status_code=403, content={"error": "forbidden"})

    processor = getattr(state, "update_processor", None)
    if processor is None:
        log.warning("webhook_update_dropped", reason="processor_not_configured")
        return _dropped("processor_not_configured")

    try:
        body = await request.json()
        update = Update.de_json(body, None)
    except Exception:
        log.warning("webhook_body_unparsable")
        return _dropped("bad_body")

    if update is None or update.update_id is None:  # pragma: no cover - defensive
        return _dropped("no_update_id")

    result = await dispatch_update(
        update,
        processor=processor,
        session_factory=state.session_factory,
        settings=cfg,
    )
    log.debug("webhook_processed", update_id=update.update_id, result=result)
    return JSONResponse(status_code=200, content={"ok": True})
