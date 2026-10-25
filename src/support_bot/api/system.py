"""Webhook Telegram и health-эндпоинты на основном порту."""

import hmac

import structlog
from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from telegram import Update

from support_bot.api.deps import ContainerDep

log = structlog.get_logger(__name__)

router = APIRouter()


@router.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    container: ContainerDep,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> Response:
    secret = container.settings.webhook_secret
    if secret is None or not hmac.compare_digest(
        (x_telegram_bot_api_secret_token or "").encode(), secret.get_secret_value().encode()
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    try:
        payload = await request.json()
        update = Update.de_json(payload, request.app.state.bot_app.bot)
    except Exception:
        log.warning("webhook_bad_payload", exc_info=True)
        return Response(status_code=status.HTTP_200_OK)
    # Обработка синхронно внутри запроса; ошибки логируются, но ответ всегда 200,
    # чтобы Telegram не повторял заведомо сбойный апдейт.
    await request.app.state.pipeline.handle(update)
    return Response(status_code=status.HTTP_200_OK)


@router.get("/health/live", include_in_schema=False)
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", include_in_schema=False, response_model=None)
async def ready(container: ContainerDep) -> dict[str, str] | JSONResponse:
    try:
        async with container.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        log.warning("readiness_db_failed", exc_info=True)
        return JSONResponse({"status": "db_unavailable"}, status_code=503)
    return {"status": "ok"}
