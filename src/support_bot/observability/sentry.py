from __future__ import annotations

from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from support_bot.config import Settings


def init_sentry(settings: Settings) -> None:
    dsn = settings.sentry_dsn.get_secret_value()
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        send_default_pii=False,
    )


def capture_exception(exc: BaseException, **tags: Any) -> None:
    with sentry_sdk.new_scope() as scope:
        for key, value in tags.items():
            scope.set_tag(key, value)
        sentry_sdk.capture_exception(exc)
