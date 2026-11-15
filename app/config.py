"""Application settings loaded from environment and a local ``.env`` file."""

from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.db.enums import Priority


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str
    dev_chat_id: int
    database_url: str
    bootstrap_admin_ids: Annotated[list[int], NoDecode]

    bot_mode: Literal["webhook", "polling"] = "polling"
    webhook_secret: str = ""
    webhook_url: str = ""
    webhook_update_timeout: int = 30
    init_data_max_age: int = 86400
    draft_ttl_seconds: int = 86400
    reminder_critical_seconds: int = 600
    reminder_high_seconds: int = 1800
    reminder_medium_seconds: int = 7200
    reminder_low_seconds: int = 28800
    reminder_poll_interval: int = 30
    post_sync_poll_interval: int = 10
    cleanup_poll_interval: int = 3600
    advisory_lock_timeout: int = 10
    app_port: int = 8000
    metrics_port: int = 9090
    log_level: str = "INFO"
    log_format: Literal["json", "dev"] = "json"
    sentry_dsn: str | None = None
    sentry_environment: str = "development"
    processed_updates_retention: int = 172800

    @field_validator("bootstrap_admin_ids", mode="before")
    @classmethod
    def _parse_bootstrap_admin_ids(cls, value: object) -> object:
        # ``NoDecode`` above stops pydantic-settings from JSON-decoding the raw
        # env string, so we can split the comma-separated value ourselves.
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        return value

    def reminder_interval_seconds(self, priority: Priority) -> int:
        intervals: dict[Priority, int] = {
            Priority.critical: self.reminder_critical_seconds,
            Priority.high: self.reminder_high_seconds,
            Priority.medium: self.reminder_medium_seconds,
            Priority.low: self.reminder_low_seconds,
        }
        return intervals[priority]


settings = Settings()
