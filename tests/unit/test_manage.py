from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr


async def test_webhook_registration_is_explicit(settings):
    from support_bot.manage import register_webhook

    settings.webhook_url = "https://example.org/telegram/webhook"
    settings.webhook_secret = SecretStr("test_secret")
    bot = SimpleNamespace(set_webhook=AsyncMock(return_value=True))
    await register_webhook(settings, bot)
    bot.set_webhook.assert_awaited_once_with(
        url=settings.webhook_url,
        secret_token="test_secret",
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )


async def test_webhook_registration_requires_https_and_secret(settings):
    from support_bot.manage import register_webhook

    with pytest.raises(ValueError):
        await register_webhook(settings, SimpleNamespace(set_webhook=AsyncMock()))
