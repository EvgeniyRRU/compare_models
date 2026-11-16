# tests/unit/test_log.py
import json

import structlog

from app.log import configure_logging, get_logger


def test_json_logs_with_context(capsys):
    configure_logging("INFO", "json")
    logger = get_logger("test.component")
    with structlog.contextvars.bound_contextvars(update_id=42):
        logger.info("hello", extra_key="v")
    out = capsys.readouterr().out
    record = json.loads(out.strip().splitlines()[-1])
    assert record["event"] == "hello"
    assert record["component"] == "test.component"
    assert record["update_id"] == 42
    assert record["extra_key"] == "v"
    assert "timestamp" in record


def test_context_not_leaked(capsys):
    configure_logging("INFO", "json")
    logger = get_logger("c")
    with structlog.contextvars.bound_contextvars(ticket_id=7):
        logger.info("x")
    capsys.readouterr()
    logger.info("y")
    record = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "ticket_id" not in record
