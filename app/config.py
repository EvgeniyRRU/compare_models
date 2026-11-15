import re
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str
    bot_mode: str = "polling"  # polling | webhook
    webhook_secret_token: str = ""
    dev_chat_id: int
    # NoDecode: pass the raw env string to the validator instead of JSON-parsing,
    # so "111,222" comma lists and single "1" both work.
    bootstrap_admin_ids: Annotated[list[int], NoDecode] = []

    # Storage
    database_url: str = "postgresql+asyncpg://rent:rent@localhost:5432/rent_bot"

    # HTTP
    port: int = 8000
    metrics_port: int = 9090

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # json | console

    # Mini App auth
    auth_date_max_age_seconds: int = 3600
    web_app_url: str = ""

    # Timeouts / intervals
    process_update_timeout_seconds: float = 30
    post_sync_interval_seconds: float = 30
    reminder_poll_interval_seconds: float = 30
    advisory_lock_timeout_seconds: float = 10
    tg_retry_attempts: int = 5
    draft_ttl_hours: float = 24
    processed_updates_ttl_hours: float = 48

    # Reminder intervals by priority, minutes
    reminder_interval_critical_min: int = 10
    reminder_interval_high_min: int = 30
    reminder_interval_medium_min: int = 120
    reminder_interval_low_min: int = 480

    # Monitoring
    sentry_dsn: str = ""

    @field_validator("bot_mode")
    @classmethod
    def _bot_mode(cls, v: str) -> str:
        if v not in {"polling", "webhook"}:
            raise ValueError("BOT_MODE must be 'polling' or 'webhook'")
        return v

    @field_validator("log_format")
    @classmethod
    def _log_format(cls, v: str) -> str:
        if v not in {"json", "console"}:
            raise ValueError("LOG_FORMAT must be 'json' or 'console'")
        return v

    @field_validator("bootstrap_admin_ids", mode="before")
    @classmethod
    def _comma_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in re.split(r"[,;\s]+", v.strip()) if x]
        if isinstance(v, int):  # single "BOOTSTRAP_ADMIN_IDS=1" parsed as JSON scalar
            return [v]
        return v

    def reminder_interval_minutes(self, priority: str) -> int:
        return {
            "critical": self.reminder_interval_critical_min,
            "high": self.reminder_interval_high_min,
            "medium": self.reminder_interval_medium_min,
            "low": self.reminder_interval_low_min,
        }[priority]


settings = Settings()
