import sentry_sdk

from app.config import Settings


def init_sentry(settings: Settings) -> None:
    if not settings.sentry_dsn:
        return  # Sentry disabled: optional DSN
    # FastAPI and SQLAlchemy integrations are auto-enabled by sentry-sdk when the
    # corresponding packages are installed.
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=0.0,
        send_default_pii=False,
    )
