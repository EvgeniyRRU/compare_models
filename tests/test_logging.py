"""Tests for :mod:`app.logging`."""

import json
from collections.abc import Iterator

import pytest
import structlog
from pydantic import ValidationError

import app.logging as app_logging
from app.config import Settings
from app.config import settings as app_settings
from app.logging import bind_contextvars, clear_contextvars, get_logger, setup_logging


@pytest.fixture
def reset_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset structlog globals and module flags so each test starts clean."""
    monkeypatch.setattr(app_logging, "_configured", False)
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()
    clear_contextvars()


def test_setup_logging_json_emits_contextvars(
    reset_logging: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``setup_logging()`` with json → structlog logger emits JSON with contextvars."""
    monkeypatch.setattr(app_settings, "log_format", "json")
    setup_logging()

    logger = get_logger()
    bind_contextvars(ticket_id=42)
    logger.info("hello")
    clear_contextvars()
    logger.info("after")

    out = capsys.readouterr().out
    lines = [json.loads(line) for line in out.strip().split("\n")]
    assert lines[0]["ticket_id"] == 42
    assert "ticket_id" not in lines[1]


def test_get_logger_binds_component(reset_logging: None) -> None:
    """``get_logger("x")`` binds ``component="x"`` on the returned logger."""
    logger = get_logger("webhook")
    assert getattr(logger, "_context", {}) == {"component": "webhook"}

    logger2 = get_logger()
    assert getattr(logger2, "_context", {}) == {}


def test_log_level_rejects_invalid() -> None:
    """``log_level`` Literal rejects an invalid value on ``Settings`` instantiation."""
    with pytest.raises(ValidationError):
        Settings(
            bot_token="t",
            dev_chat_id=1,
            database_url="postgresql+asyncpg://u:p@h/db",
            bootstrap_admin_ids=[1],
            log_level="INVALID",
            _env_file=None,
        )


def test_setup_logging_idempotent(reset_logging: None) -> None:
    """Calling ``setup_logging`` twice is a safe no-op."""
    setup_logging()
    setup_logging()
