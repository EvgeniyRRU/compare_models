from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bot_token: SecretStr = SecretStr("")
    bot_mode: Literal["webhook", "polling"] = "polling"
    telegram_webhook_secret: SecretStr = SecretStr("")
    dev_chat_id: int = 0
    bootstrap_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    database_url: str = "postgresql+asyncpg://support:support@localhost:5433/support"

    mini_app_url: str = ""
    init_data_max_age_seconds: int = 86_400

    reminder_critical_minutes: int = 10
    reminder_high_minutes: int = 30
    reminder_medium_minutes: int = 120
    reminder_low_minutes: int = 480
    reminder_poll_seconds: float = 30.0
    reminder_batch_size: int = 50

    post_sync_poll_seconds: float = 15.0
    post_sync_lock_timeout_seconds: float = 10.0
    post_sync_batch_size: int = 20

    cleanup_poll_seconds: float = 3600.0
    draft_ttl_hours: int = 24
    updates_retention_hours: int = 48

    webhook_timeout_seconds: float = 25.0
    workers_enabled: bool = True

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    sentry_dsn: SecretStr = SecretStr("")
    sentry_environment: str = "dev"
    sentry_traces_sample_rate: float = 0.0

    metrics_enabled: bool = True
    metrics_port: int = 9100

    @field_validator("bootstrap_admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            parts = [part for part in re.split(r"[,\s]+", value.strip()) if part]
            return [int(part) for part in parts]
        return value

    @property
    def database_dsn(self) -> str:
        """DSN usable by asyncpg directly (without SQLAlchemy driver suffix)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    @property
    def bot_token_value(self) -> str:
        return self.bot_token.get_secret_value()

    @property
    def webhook_secret_value(self) -> str:
        return self.telegram_webhook_secret.get_secret_value()

    def validate_runtime(self) -> None:
        """Fail fast on configuration that cannot work in production."""
        if not self.bot_token_value:
            raise RuntimeError("BOT_TOKEN is not configured")
        if self.bot_mode == "webhook" and not self.webhook_secret_value:
            raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")
        if self.dev_chat_id == 0:
            raise RuntimeError("DEV_CHAT_ID is not configured")


@lru_cache
def get_settings() -> Settings:
    return Settings()
