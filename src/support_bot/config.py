"""Конфигурация приложения: переменные окружения с фолбэком на локальный `.env`."""

from datetime import timedelta
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from support_bot.domain.enums import Priority


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: SecretStr
    bot_mode: Literal["webhook", "polling"] = "webhook"
    webhook_secret: SecretStr | None = None
    webhook_url: str | None = None
    webapp_url: str | None = None
    dev_chat_id: int
    bootstrap_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    update_timeout_seconds: float = 25.0
    telegram_max_attempts: int = 5
    telegram_backoff_base_seconds: float = 1.0
    telegram_backoff_max_seconds: float = 30.0

    # База данных
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/support_bot"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # Форма создания обращения
    draft_ttl_hours: float = 24.0

    # Синхронизация постов
    post_sync_interval_seconds: float = 30.0
    post_sync_lock_timeout_seconds: float = 10.0
    post_sync_batch_size: int = 50

    # Напоминания
    reminder_poll_interval_seconds: float = 30.0
    reminder_batch_size: int = 100
    reminder_critical_minutes: float = 10.0
    reminder_high_minutes: float = 30.0
    reminder_medium_minutes: float = 120.0
    reminder_low_minutes: float = 480.0

    # Очистка
    cleanup_interval_seconds: float = 600.0
    processed_updates_ttl_hours: float = 48.0

    # Mini App
    init_data_max_age_seconds: int = 86400

    # Логирование и мониторинг
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    sentry_dsn: str | None = None
    sentry_environment: str = "production"
    sentry_traces_sample_rate: float = 0.0
    metrics_port: int = 9000

    @field_validator("bootstrap_admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.replace(";", ",").split(",") if part.strip()]
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("sentry_dsn", "webhook_url", "webapp_url", "webhook_secret", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def draft_ttl(self) -> timedelta:
        return timedelta(hours=self.draft_ttl_hours)

    @property
    def processed_updates_ttl(self) -> timedelta:
        return timedelta(hours=self.processed_updates_ttl_hours)

    @property
    def reminder_intervals(self) -> dict[Priority, timedelta]:
        return {
            Priority.CRITICAL: timedelta(minutes=self.reminder_critical_minutes),
            Priority.HIGH: timedelta(minutes=self.reminder_high_minutes),
            Priority.MEDIUM: timedelta(minutes=self.reminder_medium_minutes),
            Priority.LOW: timedelta(minutes=self.reminder_low_minutes),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
