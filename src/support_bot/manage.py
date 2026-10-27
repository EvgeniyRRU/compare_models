"""Explicit operator actions; never register a webhook from each replica."""

import argparse
import asyncio
from urllib.parse import urlparse

from telegram import Bot

from support_bot.config import Settings


async def register_webhook(settings: Settings, bot: Bot) -> None:
    url = urlparse(settings.webhook_url)
    if url.scheme != "https" or not url.hostname or not settings.webhook_secret.get_secret_value():
        raise ValueError("Set an HTTPS WEBHOOK_URL and a nonempty WEBHOOK_SECRET")
    await bot.set_webhook(
        url=settings.webhook_url,
        secret_token=settings.webhook_secret.get_secret_value(),
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


async def run() -> None:
    settings = Settings()
    async with Bot(settings.bot_token.get_secret_value()) as bot:
        await register_webhook(settings, bot)
    print("Webhook registered")


def main() -> None:
    parser = argparse.ArgumentParser(description="Support bot operator commands")
    parser.add_argument("command", choices=["register-webhook"])
    parser.parse_args()
    asyncio.run(run())


if __name__ == "__main__":
    main()
