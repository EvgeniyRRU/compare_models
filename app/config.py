from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: str = Field(min_length=1)
    webhook_secret: str = Field(min_length=1)
    database_url: str = Field(min_length=1)
    dev_chat_id: int
    mini_app_url: str = Field(min_length=1)
    bot_mode: Literal["webhook", "polling"] = "webhook"
    bootstrap_admin_ids: list[int] = Field(default_factory=list)
    draft_ttl_hours: int = Field(default=24, gt=0)
    init_data_max_age_seconds: int = Field(default=3600, gt=0)
    webhook_timeout_seconds: int = Field(default=20, gt=0)
    reminder_poll_seconds: int = Field(default=30, gt=0)
    sync_poll_seconds: int = Field(default=30, gt=0)
    metrics_port: int = Field(default=9090, ge=1, le=65535)
    sentry_dsn: str | None = None
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    reminder_critical_minutes: int = Field(default=10, gt=0)
    reminder_high_minutes: int = Field(default=30, gt=0)
    reminder_medium_minutes: int = Field(default=120, gt=0)
    reminder_low_minutes: int = Field(default=480, gt=0)
