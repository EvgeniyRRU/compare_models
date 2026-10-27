"""Environment configuration; production secrets are supplied by the operator."""

import json
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: SecretStr
    database_url: str = "postgresql+asyncpg://support:support@localhost:5432/support"
    bot_mode: Literal["polling", "webhook"] = "polling"
    dev_chat_id: int = -1
    bootstrap_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    mini_app_url: str = ""
    webhook_url: str = ""
    webhook_secret: SecretStr = SecretStr("")
    update_timeout_seconds: float = Field(default=30, gt=0)
    init_data_max_age_seconds: int = Field(default=3600, gt=0)
    auth_clock_skew_seconds: int = Field(default=30, ge=0)
    draft_ttl_hours: float = Field(default=24, gt=0)
    reminder_critical_seconds: int = Field(default=600, gt=0)
    reminder_high_seconds: int = Field(default=1800, gt=0)
    reminder_medium_seconds: int = Field(default=7200, gt=0)
    reminder_low_seconds: int = Field(default=28800, gt=0)
    reminder_poll_seconds: float = Field(default=15, gt=0)
    post_sync_poll_seconds: float = Field(default=10, gt=0)
    cleanup_poll_seconds: float = Field(default=600, gt=0)
    worker_batch_size: int = Field(default=100, ge=1, le=1000)
    post_lock_timeout_seconds: float = Field(default=10, gt=0)
    telegram_retry_attempts: int = Field(default=3, ge=1, le=10)
    telegram_retry_base_seconds: float = Field(default=0.5, ge=0)
    telegram_timeout_seconds: float = Field(default=10, gt=0)
    db_pool_size: int = Field(default=10, ge=2)
    db_max_overflow: int = Field(default=10, ge=0)
    http_host: str = "0.0.0.0"
    http_port: int = Field(default=8000, ge=1, le=65535)
    metrics_host: str = "0.0.0.0"
    metrics_port: int = Field(default=9000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    sentry_dsn: str = ""
    sentry_environment: str = "development"

    @field_validator("bootstrap_admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            if value.strip().startswith("["):
                return json.loads(value)
            return [int(item.strip()) for item in value.split(",") if item.strip()]
        return value

    @field_validator("database_url")
    @classmethod
    def async_postgres(cls, value: str) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use postgresql+asyncpg://")
        return value

    @model_validator(mode="after")
    def validate_modes(self) -> Settings:
        import re

        secret = self.webhook_secret.get_secret_value()
        if self.bot_mode == "webhook" and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
            raise ValueError("WEBHOOK_SECRET must contain 1–256 letters, digits, '_' or '-'")
        if not self.bot_token.get_secret_value().strip():
            raise ValueError("BOT_TOKEN must not be empty")
        if self.http_port == self.metrics_port:
            raise ValueError("Metrics must use a separate port")
        return self

    def reminder_seconds(self, priority: str) -> int:
        return {
            "critical": self.reminder_critical_seconds,
            "high": self.reminder_high_seconds,
            "medium": self.reminder_medium_seconds,
            "low": self.reminder_low_seconds,
        }[priority]
