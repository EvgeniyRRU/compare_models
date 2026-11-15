"""Sentry initialisation for the application."""

from app.config import settings


def init_sentry() -> None:
    """Initialise Sentry if ``settings.sentry_dsn`` is provided.

    When ``sentry_dsn`` is ``None`` or empty, this is a no-op so the
    application can run standalone without Sentry.
    """
    if not settings.sentry_dsn:
        return

    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
    )
