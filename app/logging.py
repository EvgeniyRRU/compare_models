"""Structured logging: structlog configured to emit JSON (or human-readable) to stdout.

The contextvars-based tags (``request_id``, ``update_id``, ``telegram_user_id``,
``ticket_id``, ``component``) are merged into every log entry via
``structlog.contextvars.merge_contextvars``. Callers attach them with
``bind_contextvars`` / ``bound_contextvars`` and clear them with
``clear_contextvars``.
"""

import logging
from typing import Any

import structlog
from structlog.contextvars import (  # noqa: F401 — re-exported
    bind_contextvars,
    bound_contextvars,
    clear_contextvars,
    merge_contextvars,
    unbind_contextvars,
)
from structlog.dev import ConsoleRenderer
from structlog.processors import (
    JSONRenderer,
    StackInfoRenderer,
    TimeStamper,
    add_log_level,
    format_exc_info,
)
from structlog.typing import Processor

from app.config import settings

__all__ = [
    "bind_contextvars",
    "bound_contextvars",
    "clear_contextvars",
    "get_logger",
    "setup_logging",
    "unbind_contextvars",
]

_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_configured = False


def setup_logging() -> None:
    """Configure structlog (and the stdlib root level) once, idempotently."""
    global _configured
    if _configured:
        return

    level = _LOG_LEVELS[settings.log_level]

    shared_processors: list[Processor] = [
        merge_contextvars,
        add_log_level,
        TimeStamper(fmt="iso"),
        StackInfoRenderer(),
        format_exc_info,
    ]

    renderer: Processor = (
        JSONRenderer(ensure_ascii=False) if settings.log_format == "json" else ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Existing stdlib ``logging.getLogger`` call sites keep working; make sure
    # they respect the configured level too.
    logging.getLogger().setLevel(level)

    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger bound with ``component=name`` (when given)."""
    if name is not None:
        return structlog.get_logger(component=name)
    return structlog.get_logger()
