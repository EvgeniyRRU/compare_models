from __future__ import annotations

import asyncio

import structlog
from telegram import Update
from telegram.ext import Application

from support_bot.bot.common import BotServices
from support_bot.observability.metrics import observe_error, observe_update
from support_bot.observability.sentry import capture_exception
from support_bot.services.updates import try_mark_processed

logger = structlog.get_logger(__name__)


async def polling_loop(
    application: Application,
    services: BotServices,
    stop_event: asyncio.Event,
) -> None:
    offset: int | None = None
    while not stop_event.is_set():
        try:
            updates = await application.bot.get_updates(
                offset=offset,
                timeout=30,
                allowed_updates=list(Update.ALL_TYPES),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("getUpdates failed", component="polling")
            observe_error("polling")
            capture_exception(exc, component="polling")
            await _sleep_or_stop(stop_event, 5.0)
            continue

        for update in updates:
            offset = update.update_id + 1
            try:
                is_new = await try_mark_processed(services.session_factory, update.update_id)
            except Exception as exc:
                logger.exception("update dedup failed", component="polling")
                observe_error("polling")
                capture_exception(exc, component="polling")
                continue
            observe_update(duplicate=not is_new)
            if not is_new:
                logger.info(
                    "duplicate update skipped",
                    component="polling",
                    update_id=update.update_id,
                )
                continue

            context: dict[str, object] = {
                "update_id": update.update_id,
                "component": "polling",
            }
            if update.effective_user is not None:
                context["telegram_user_id"] = update.effective_user.id
            with structlog.contextvars.bound_contextvars(**context):
                try:
                    await application.process_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        "update processing failed",
                        component="polling",
                        update_id=update.update_id,
                    )
                    observe_error("polling")
                    capture_exception(exc, component="polling", update_id=update.update_id)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        pass
