"""Sentry: включается только при заданном SENTRY_DSN."""

from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from support_bot.config import Settings


def init_sentry(settings: Settings) -> bool:
    if not settings.sentry_dsn:
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
            SqlalchemyIntegration(),
            # Ошибки отправляем явно через capture_exception, логи событиями не дублируем.
            LoggingIntegration(event_level=None),
        ],
    )
    return True


def capture_exception(exc: BaseException, **tags: Any) -> None:
    """Отправить исключение в Sentry с тегами (no-op, если Sentry выключен)."""
    with sentry_sdk.new_scope() as scope:
        for key, value in tags.items():
            if value is not None:
                scope.set_tag(key, str(value))
        sentry_sdk.capture_exception(exc)
