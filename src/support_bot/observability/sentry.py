from typing import Any

import sentry_sdk
import structlog
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from structlog.contextvars import get_contextvars

from support_bot.config import Settings


def configure_sentry(settings: Settings) -> Any:
    if not settings.sentry_dsn:
        return None
    return sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        send_default_pii=False,
        traces_sample_rate=0.0,
    )


def capture_error(error: BaseException, *, component: str) -> None:
    structlog.get_logger().error(
        "operation_failed",
        component=component,
        error_type=type(error).__name__,
        exc_info=(type(error), error, error.__traceback__),
    )
    with sentry_sdk.new_scope() as scope:
        for key, value in {**get_contextvars(), "component": component}.items():
            if value is not None:
                scope.set_tag(key, value)
        sentry_sdk.capture_exception(error)
    from support_bot.observability.metrics import metrics

    metrics.errors.labels(component=component).inc()
