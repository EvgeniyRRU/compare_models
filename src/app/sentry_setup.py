import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration


def setup_sentry(dsn: str, environment: str) -> None:
    if not dsn:
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        integrations=[StarletteIntegration(), FastApiIntegration(), SqlalchemyIntegration()],
    )


def capture(exc: BaseException, **tags: object) -> None:
    with sentry_sdk.new_scope() as scope:
        for k, v in tags.items():
            scope.set_tag(k, str(v))
        sentry_sdk.capture_exception(exc)
