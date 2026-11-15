import logging
import sys

import structlog


def configure_logging(level: str, log_format: str) -> None:
    """Structured JSON logs to stdout with contextvars tags.

    Tags bound via structlog.contextvars.bind_contextvars (request_id, update_id,
    telegram_user_id, ticket_id, component) are merged into every record within the task.
    """
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    # add_logger_name only works for stdlib records; PrintLogger has no .name.
    foreign_pre_chain: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=[*structlog_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
            foreign_pre_chain=foreign_pre_chain,
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
