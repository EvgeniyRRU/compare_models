import os

import pytest

from support_bot.config import Settings


def base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith(("BOT_", "BOOTSTRAP_", "DATABASE_", "DEV_CHAT", "LOG_", "SENTRY_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("BOT_TOKEN", "123:abc")
    monkeypatch.setenv("DEV_CHAT_ID", "-1001")


def test_loads_required_values_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    base_env(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.bot_mode == "polling"
    assert settings.dev_chat_id == -1001
    assert settings.bootstrap_admin_ids == []
    assert settings.draft_ttl_hours == 24
    assert settings.processed_updates_retention_hours == 48
    assert settings.sentry_dsn is None


def test_parses_comma_separated_bootstrap_admin_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    base_env(monkeypatch)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_IDS", "10, 20,30")

    settings = Settings(_env_file=None)

    assert settings.bootstrap_admin_ids == [10, 20, 30]


def test_reminder_intervals_follow_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    base_env(monkeypatch)

    settings = Settings(_env_file=None)

    assert settings.reminder_interval_minutes("critical") == 10
    assert settings.reminder_interval_minutes("high") == 30
    assert settings.reminder_interval_minutes("medium") == 120
    assert settings.reminder_interval_minutes("low") == 480


def test_rejects_unknown_bot_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    base_env(monkeypatch)
    monkeypatch.setenv("BOT_MODE", "carrier-pigeon")

    with pytest.raises(ValueError):
        Settings(_env_file=None)


def test_webhook_mode_requires_public_url_and_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    base_env(monkeypatch)
    monkeypatch.setenv("BOT_MODE", "webhook")

    with pytest.raises(ValueError):
        Settings(_env_file=None)
