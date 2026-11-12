from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/support"
    bot_token: str = ""
    bot_mode: Literal["polling", "webhook"] = "polling"
    telegram_webhook_secret: str = ""
    developer_chat_id: int = Field(
        default=0, validation_alias=AliasChoices("DEV_CHAT_ID", "DEVELOPER_CHAT_ID")
    )
    miniapp_url: str = "https://example.invalid"
    bootstrap_admin_ids: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    metrics_port: int = 9090
    log_level: str = "INFO"
    log_format: str = "json"
    sentry_dsn: str | None = None
    webhook_timeout_seconds: float = Field(default=20, gt=0)
    init_data_max_age_seconds: int = 3600
    update_retention_hours: int = 48
    draft_ttl_hours: int = 24
    reminder_poll_seconds: int = 15
    sync_poll_seconds: int = 5
    critical_reminder_minutes: int = Field(default=10, gt=0)
    high_reminder_minutes: int = Field(default=30, gt=0)
    medium_reminder_minutes: int = Field(default=120, gt=0)
    low_reminder_minutes: int = Field(default=480, gt=0)

    @property
    def bootstrap_ids(self) -> set[int]:
        return {
            int(value.strip()) for value in self.bootstrap_admin_ids.split(",") if value.strip()
        }


@lru_cache
def get_settings() -> Settings:
    return Settings()
