"""Служебные команды: `python -m support_bot.cli <команда>`.

set-webhook     зарегистрировать WEBHOOK_URL с секретом WEBHOOK_SECRET
delete-webhook  удалить webhook (например, перед локальным polling)
webhook-info    показать текущее состояние webhook
set-menu-button установить кнопку меню Mini App (WEBAPP_URL)
"""

import argparse
import asyncio

from telegram import Bot, MenuButtonWebApp, Update, WebAppInfo

from support_bot.config import get_settings


async def _run(command: str) -> None:
    settings = get_settings()
    async with Bot(settings.bot_token.get_secret_value()) as bot:
        match command:
            case "set-webhook":
                if not settings.webhook_url or settings.webhook_secret is None:
                    raise SystemExit("Нужны WEBHOOK_URL и WEBHOOK_SECRET")
                await bot.set_webhook(
                    url=settings.webhook_url,
                    secret_token=settings.webhook_secret.get_secret_value(),
                    allowed_updates=Update.ALL_TYPES,
                )
                print(f"Webhook установлен: {settings.webhook_url}")
            case "delete-webhook":
                await bot.delete_webhook()
                print("Webhook удалён")
            case "webhook-info":
                info = await bot.get_webhook_info()
                print(info.to_json())
            case "set-menu-button":
                if not settings.webapp_url:
                    raise SystemExit("Нужен WEBAPP_URL")
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp("Обращения", WebAppInfo(settings.webapp_url))
                )
                print("Кнопка меню установлена")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="support_bot.cli",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command", choices=["set-webhook", "delete-webhook", "webhook-info", "set-menu-button"]
    )
    asyncio.run(_run(parser.parse_args().command))


if __name__ == "__main__":
    main()
