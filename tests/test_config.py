"""Tests for :mod:`app.config`."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.db.enums import Priority

_REQUIRED_ENV = {
    "BOT_TOKEN": "test-token",
    "DEV_CHAT_ID": "42",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost:5432/db",
    "BOOTSTRAP_ADMIN_IDS": "1,2",
}

# Every key the Settings model recognises, so ambient environment variables
# cannot leak into (and destabilise) the default-value assertions below.
_ALL_KEYS = tuple(Settings.model_fields)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Provide a clean baseline of required env vars for Settings.

    Clears every recognised key first so ambient env vars never leak in.
    """
    for key in _ALL_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    return monkeypatch


def make_settings() -> Settings:
    # ``_env_file=None`` disables reading a local ``.env`` so tests are isolated.
    return Settings(_env_file=None)


def test_bootstrap_admin_ids_comma_split(env: pytest.MonkeyPatch) -> None:
    env.setenv("BOOTSTRAP_ADMIN_IDS", "123, 456, 789")
    assert make_settings().bootstrap_admin_ids == [123, 456, 789]


def test_bootstrap_admin_ids_single_value(env: pytest.MonkeyPatch) -> None:
    env.setenv("BOOTSTRAP_ADMIN_IDS", "42")
    assert make_settings().bootstrap_admin_ids == [42]


def test_required_field_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEV_CHAT_ID", "42")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_IDS", "1")
    with pytest.raises(ValidationError):
        make_settings()


def test_default_values(env: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    assert settings.bot_mode == "polling"
    assert settings.webhook_secret == ""
    assert settings.webhook_url == ""
    assert settings.webhook_update_timeout == 30
    assert settings.init_data_max_age == 86400
    assert settings.draft_ttl_seconds == 86400
    assert settings.reminder_critical_seconds == 600
    assert settings.reminder_high_seconds == 1800
    assert settings.reminder_medium_seconds == 7200
    assert settings.reminder_low_seconds == 28800
    assert settings.reminder_poll_interval == 30
    assert settings.post_sync_poll_interval == 10
    assert settings.cleanup_poll_interval == 3600
    assert settings.advisory_lock_timeout == 10
    assert settings.app_port == 8000
    assert settings.metrics_port == 9090
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.sentry_dsn is None
    assert settings.sentry_environment == "development"
    assert settings.processed_updates_retention == 172800


def test_reminder_interval_seconds_mapping(env: pytest.MonkeyPatch) -> None:
    settings = make_settings()
    assert settings.reminder_interval_seconds(Priority.critical) == 600
    assert settings.reminder_interval_seconds(Priority.high) == 1800
    assert settings.reminder_interval_seconds(Priority.medium) == 7200
    assert settings.reminder_interval_seconds(Priority.low) == 28800


def test_reminder_interval_seconds_uses_configured_values(env: pytest.MonkeyPatch) -> None:
    env.setenv("REMINDER_CRITICAL_SECONDS", "111")
    env.setenv("REMINDER_HIGH_SECONDS", "222")
    env.setenv("REMINDER_MEDIUM_SECONDS", "333")
    env.setenv("REMINDER_LOW_SECONDS", "444")
    settings = make_settings()
    assert settings.reminder_interval_seconds(Priority.critical) == 111
    assert settings.reminder_interval_seconds(Priority.high) == 222
    assert settings.reminder_interval_seconds(Priority.medium) == 333
    assert settings.reminder_interval_seconds(Priority.low) == 444
