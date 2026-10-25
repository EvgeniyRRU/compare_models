"""PTB Application без Updater: апдейты подаются через общий UpdatePipeline."""

import asyncio
from typing import Any

import structlog
from telegram import MenuButtonWebApp, Update, WebAppInfo
from telegram.ext import Application, ApplicationBuilder

from support_bot.config import Settings
from support_bot.telegram.pipeline import UpdatePipeline

log = structlog.get_logger(__name__)

type PTBApplication = Application[Any, Any, Any, Any, Any, Any]


def build_application(settings: Settings) -> PTBApplication:
    return (
        ApplicationBuilder()
        .token(settings.bot_token.get_secret_value())
        .updater(None)
        .job_queue(None)
        .connect_timeout(10)
        .read_timeout(20)
        .write_timeout(20)
        .get_updates_read_timeout(40)
        .build()
    )


async def configure_menu_button(app: PTBApplication, settings: Settings) -> None:
    """Кнопка меню для открытия Mini App (действует в личных чатах)."""
    if not settings.webapp_url:
        return
    try:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp("Обращения", WebAppInfo(settings.webapp_url))
        )
    except Exception:
        log.warning("set_menu_button_failed", exc_info=True)


async def run_polling(
    app: PTBApplication, pipeline: UpdatePipeline, poll_timeout: int = 30
) -> None:
    """Режим BOT_MODE=polling для локальной разработки: свой цикл getUpdates."""
    await app.bot.delete_webhook()
    offset: int | None = None
    log.info("polling_started")
    while True:
        try:
            updates = await app.bot.get_updates(
                offset=offset,
                timeout=poll_timeout,
                read_timeout=poll_timeout + 10,
                allowed_updates=Update.ALL_TYPES,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("get_updates_failed", exc_info=True)
            await asyncio.sleep(3)
            continue
        for update in updates:
            try:
                await pipeline.handle(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Например, БД недоступна при записи дедупликации: апдейт не обработан,
                # offset не сдвигаем и повторим позже (повторная обработка исключена дедупом).
                log.warning("polling_update_failed", update_id=update.update_id, exc_info=True)
                await asyncio.sleep(3)
                break
            offset = update.update_id + 1
