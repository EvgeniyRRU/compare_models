from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    bot_token: SecretStr
    dev_chat_id: int
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_url: str | None = None
    webhook_secret_token: SecretStr | None = None
    mini_app_url: str | None = None
    bootstrap_admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    draft_ttl_hours: int = 24
    update_timeout_seconds: float = 30.0
    processed_updates_retention_hours: int = 48
    init_data_max_age_seconds: int = 86400

    reminder_critical_minutes: int = 10
    reminder_high_minutes: int = 30
    reminder_medium_minutes: int = 120
    reminder_low_minutes: int = 480
    reminder_poll_seconds: float = 30.0

    post_sync_poll_seconds: float = 30.0
    post_sync_lock_timeout_seconds: float = 10.0

    metrics_port: int = 9100

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    sentry_dsn: str | None = None

    @field_validator("bootstrap_admin_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _check_webhook(self) -> Self:
        if self.bot_mode == "webhook" and not (self.webhook_url and self.webhook_secret_token):
            raise ValueError("BOT_MODE=webhook requires WEBHOOK_URL and WEBHOOK_SECRET_TOKEN")
        return self

    def reminder_interval_minutes(self, priority: str) -> int:
        intervals = {
            "critical": self.reminder_critical_minutes,
            "high": self.reminder_high_minutes,
            "medium": self.reminder_medium_minutes,
            "low": self.reminder_low_minutes,
        }
        if priority not in intervals:
            raise ValueError(f"Unknown priority: {priority}")
        return intervals[priority]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
