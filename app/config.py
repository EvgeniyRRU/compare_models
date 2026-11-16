from typing import Annotated, Any

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REMINDER_PRIORITIES = ("critical", "high", "medium", "low")


def _split_csv(value: Any) -> Any:
    # NoDecode: pydantic-settings передаёт строку из env как есть —
    # разбираем "111, 222" в ["111", "222"] перед валидацией list[int]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    bot_token: str
    bot_mode: str = "polling"  # "polling" | "webhook"
    webhook_url: str = ""
    webhook_secret_token: str = ""
    dev_chat_id: int
    bootstrap_admin_ids: Annotated[list[int], NoDecode, BeforeValidator(_split_csv)] = []
    mini_app_url: str = ""

    # Database
    database_url: str

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "pretty"

    # Monitoring
    sentry_dsn: str | None = None
    metrics_port: int = Field(default=9464, ge=1, le=65535)

    # Лимиты и интервалы
    update_timeout_seconds: float = 5.0
    draft_ttl_hours: int = 24
    initdata_max_age_seconds: int = 86400
    post_sync_interval_seconds: float = 15.0
    reminder_poll_interval_seconds: float = 30.0
    reminder_interval_critical_seconds: int = 600
    reminder_interval_high_seconds: int = 1800
    reminder_interval_medium_seconds: int = 7200
    reminder_interval_low_seconds: int = 28800

    def reminder_interval(self, priority: str) -> int:
        return {
            "critical": self.reminder_interval_critical_seconds,
            "high": self.reminder_interval_high_seconds,
            "medium": self.reminder_interval_medium_seconds,
            "low": self.reminder_interval_low_seconds,
        }[priority]

    def validate_modes(self) -> None:
        if self.bot_mode not in ("polling", "webhook"):
            raise ValueError(f"invalid BOT_MODE: {self.bot_mode}")
        if self.bot_mode == "webhook":
            if not self.webhook_url:
                raise ValueError("WEBHOOK_URL required when BOT_MODE=webhook")
            if not self.webhook_secret_token:
                raise ValueError("WEBHOOK_SECRET_TOKEN required when BOT_MODE=webhook")
