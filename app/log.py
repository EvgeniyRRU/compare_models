"""Структурированные JSON-логи (structlog). Контекстные теги — через contextvars."""

import logging
import sys
from typing import cast

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars, unbind_contextvars


def configure_logging(level: str, fmt: str) -> None:
    clear_contextvars()
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(stream=sys.stdout, level=numeric_level, format="%(message)s")
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


def get_logger(component: str) -> structlog.BoundLogger:
    # ВАЖНО: возвращаем ленивый прокси БЕЗ немедленного .bind().
    # Цепочка процессоров резолвится в момент каждого логирования — с ТЕКУЩИМИ
    # настройками configure_logging(). Модульные `log = get_logger(...)` создаются
    # при import (до lifespan), и немедленный bind «заморозил» бы дефолтный
    # конфиг structlog (не JSON) — нарушение SPEC.
    return cast(structlog.BoundLogger, structlog.get_logger(component=component))


__all__ = ["configure_logging", "get_logger", "bind_contextvars", "unbind_contextvars"]
