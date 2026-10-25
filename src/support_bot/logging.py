"""Структурированные логи (structlog) в stdout с тегами из contextvars.

Теги request_id, update_id, telegram_user_id, ticket_id, component привязываются через
structlog.contextvars и попадают во все записи в рамках запроса или апдейта, включая
записи стандартного logging (uvicorn, PTB, SQLAlchemy).
"""

import logging
import sys
from typing import Literal

import structlog
from structlog.types import Processor


def configure_logging(level: str = "INFO", fmt: Literal["json", "console"] = "json") -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    final: list[Processor]
    if fmt == "json":
        final = [
            structlog.processors.dict_tracebacks,
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]
    else:
        final = [structlog.dev.ConsoleRenderer()]

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder()],
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *final],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    # Доступ логирует middleware (с шаблоном маршрута и request_id).
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # httpx (PTB) логирует каждый запрос к Bot API на INFO, а URL содержит токен бота.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
