import pytest

from app.config import Settings


def test_defaults(monkeypatch):
    # test-сервис compose ставит LOG_FORMAT=pretty в env — уберём, чтобы проверить реальный дефолт
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    # host .env пробрасывается в контейнер через compose env_file — уберём, чтобы проверить дефолт
    monkeypatch.delenv("BOOTSTRAP_ADMIN_IDS", raising=False)
    s = Settings(
        bot_token="1:AA",
        dev_chat_id=-100,
        database_url="postgresql+asyncpg://u:p@h/db",
        _env_file=None,
    )
    assert s.bot_mode == "polling"
    assert s.log_format == "json"
    assert s.metrics_port == 9464
    assert s.draft_ttl_hours == 24
    assert s.update_timeout_seconds == 5.0
    assert s.bootstrap_admin_ids == []
    assert s.reminder_interval("critical") == 600
    assert s.reminder_interval("low") == 28800


def test_bootstrap_admin_ids_from_env(monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_IDS", "111, 222")
    s = Settings(
        bot_token="1:AA",
        dev_chat_id=-100,
        database_url="postgresql+asyncpg://u:p@h/db",
        _env_file=None,
    )
    assert s.bootstrap_admin_ids == [111, 222]


def test_validate_modes_rejects_unknown_mode():
    s = Settings(
        bot_token="x", dev_chat_id=1, database_url="d", _env_file=None, bot_mode="webhooks"
    )
    with pytest.raises(ValueError, match="invalid BOT_MODE"):
        s.validate_modes()


def test_validate_modes_webhook_requires_url():
    s = Settings(
        bot_token="x",
        dev_chat_id=1,
        database_url="d",
        _env_file=None,
        bot_mode="webhook",
        webhook_url="",
    )
    with pytest.raises(ValueError, match="WEBHOOK_URL required"):
        s.validate_modes()


def test_validate_modes_webhook_requires_secret():
    s = Settings(
        bot_token="x",
        dev_chat_id=1,
        database_url="d",
        _env_file=None,
        bot_mode="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret_token="",
    )
    with pytest.raises(ValueError, match="WEBHOOK_SECRET_TOKEN required"):
        s.validate_modes()


def test_validate_modes_accepts_valid_configs():
    polling = Settings(bot_token="x", dev_chat_id=1, database_url="d", _env_file=None)
    polling.validate_modes()
    webhook = Settings(
        bot_token="x",
        dev_chat_id=1,
        database_url="d",
        _env_file=None,
        bot_mode="webhook",
        webhook_url="https://example.com/hook",
        webhook_secret_token="s",
    )
    webhook.validate_modes()
