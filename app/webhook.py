"""Telegram webhook endpoint: validate, dedup, and process one update.

Telegram redelivers an update on any non-2xx response, so this endpoint must
never surface a processing error as a 5xx. The dedup marker is committed in its
own short transaction *before* business processing, so an error downstream can
never roll the marker back (which would re-enqueue an already-processed update).
"""

import asyncio
import logging
import secrets
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from telegram import Update
from telegram.ext import Application

from app.config import settings
from app.db.session import async_session_factory
from app.services.update_dedup import mark_processed

logger = logging.getLogger(__name__)

router = APIRouter()


async def _handle_update(
    application: Application[Any, Any, Any, Any, Any, Any],
    update: Update,
) -> None:
    """Dedup and process ``update`` exactly once; never raise.

    The dedup marker is committed in a dedicated short transaction so a business
    error (logged + swallowed below) cannot roll it back. A duplicate update is a
    no-op: its id is already in ``processed_updates``.
    """
    async with async_session_factory() as session:
        if not await mark_processed(session, update.update_id):
            # Already seen: a retry of an update that was processed before.
            return
        await session.commit()

    try:
        await asyncio.wait_for(
            application.process_update(update),
            timeout=settings.webhook_update_timeout,
        )
    except Exception:  # noqa: BLE001 — the endpoint must still answer 200.
        # Sentry reporting is wired up in Stage 8; for now, log and swallow.
        logger.exception("webhook processing failed for update_id=%s", update.update_id)


@router.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    if settings.webhook_secret and not secrets.compare_digest(
        settings.webhook_secret, x_telegram_bot_api_secret_token or ""
    ):
        return Response(status_code=401)

    application = request.app.state.application
    try:
        payload: dict[str, Any] = await request.json()
        update = Update.de_json(payload, application.bot)
        await _handle_update(application, update)
    except Exception:  # noqa: BLE001 — never surface a 5xx to Telegram.
        logger.exception("webhook request failed")

    return Response(status_code=200)
