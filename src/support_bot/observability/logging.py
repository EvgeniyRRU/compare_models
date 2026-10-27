"""Structured output for stdlib loggers and structlog, with shared request tags."""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog


def configure_logging(
    level: str = "INFO",
    output_format: str = "json",
    secrets: tuple[str, ...] = (),
) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer = (
        structlog.processors.JSONRenderer()
        if output_format == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    def redact(logger: object, method: str, event: MutableMapping[str, Any]) -> dict[str, Any]:
        def clean(value: Any) -> Any:
            if isinstance(value, str):
                for secret in secrets:
                    if secret:
                        value = value.replace(secret, "[REDACTED]")
                return value
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(item) for item in value]
            return value

        return clean(event)

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            redact,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    # HTTP request logs may contain Telegram's token in its URL. Keep them off.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
