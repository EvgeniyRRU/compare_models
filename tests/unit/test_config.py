import pytest
from pydantic import ValidationError


def test_settings_validate_modes_and_defaults():
    from support_bot.config import Settings

    settings = Settings(bot_token="123:secret", database_url="postgresql+asyncpg://localhost/test")
    assert settings.bot_mode == "polling"
    assert settings.reminder_seconds("critical") == 600
    assert settings.draft_ttl_hours == 24
    assert settings.bootstrap_admin_ids == []
    assert settings.metrics_port != settings.http_port


def test_webhook_requires_secret():
    from support_bot.config import Settings

    with pytest.raises(ValidationError):
        Settings(bot_token="123:secret", bot_mode="webhook", webhook_secret="")


def test_bootstrap_comma_separated(monkeypatch):
    from support_bot.config import Settings

    monkeypatch.setenv("BOOTSTRAP_ADMIN_IDS", "12, 34")
    assert Settings(bot_token="123:secret").bootstrap_admin_ids == [12, 34]
