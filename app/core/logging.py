import logging

import structlog


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    logging.basicConfig(level=level.upper(), format="%(message)s")
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
    )
