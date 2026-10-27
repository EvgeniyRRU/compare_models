"""Single-process production entry point."""

import uvicorn

from support_bot.config import Settings
from support_bot.observability.logging import configure_logging


def main() -> None:
    settings = Settings()
    configure_logging(
        settings.log_level,
        settings.log_format,
        (settings.bot_token.get_secret_value(), settings.webhook_secret.get_secret_value()),
    )
    uvicorn.run(
        "support_bot.main:create_app",
        factory=True,
        host=settings.http_host,
        port=settings.http_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
