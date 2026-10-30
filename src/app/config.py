from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class BotMode(StrEnum):
    POLLING = "polling"
    WEBHOOK = "webhook"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str
    bot_mode: BotMode = BotMode.POLLING
    webhook_url: str = ""  # полный публичный URL .../telegram/webhook (только для webhook)
    webhook_secret: str = ""
    dev_chat_id: int
    webapp_url: str = ""  # публичный HTTPS URL Mini App
    bootstrap_admin_ids: Annotated[list[int], NoDecode] = []

    # БД
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/support_bot"

    # HTTP
    metrics_port: int = 9100
    update_timeout_seconds: float = 25.0
    initdata_max_age_seconds: int = 3600

    # Обращения и черновики
    draft_ttl_hours: int = 24
    processed_updates_ttl_hours: int = 48
    reminder_critical_minutes: int = 10
    reminder_high_minutes: int = 30
    reminder_medium_minutes: int = 120
    reminder_low_minutes: int = 480

    # Фоновые задачи
    reminder_poll_seconds: float = 30.0
    post_sync_poll_seconds: float = 15.0
    cleanup_poll_seconds: float = 600.0
    advisory_lock_timeout_seconds: float = 10.0

    # Логирование и мониторинг
    log_level: str = "INFO"
    log_format: str = "json"  # json | console
    sentry_dsn: str = ""
    environment: str = "dev"

    @field_validator("bootstrap_admin_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in v.replace(";", ",").split(",") if x.strip()]
        return v

    @property
    def reminder_intervals(self) -> dict[str, timedelta]:
        return {
            "critical": timedelta(minutes=self.reminder_critical_minutes),
            "high": timedelta(minutes=self.reminder_high_minutes),
            "medium": timedelta(minutes=self.reminder_medium_minutes),
            "low": timedelta(minutes=self.reminder_low_minutes),
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
