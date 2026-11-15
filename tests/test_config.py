import json

import pytest
import structlog

from app.config import Settings, settings


def test_comma_separated_bootstrap_ids(monkeypatch) -> None:
    monkeypatch.setenv("BOOTSTRAP_ADMIN_IDS", "11, 22;33\n44")
    s = Settings()
    assert s.bootstrap_admin_ids == [11, 22, 33, 44]


def test_reminder_interval_lookup() -> None:
    assert settings.reminder_interval_minutes("critical") == 10
    assert settings.reminder_interval_minutes("low") == 480


def test_invalid_bot_mode(monkeypatch) -> None:
    monkeypatch.setenv("BOT_MODE", "carrier-pigeon")
    with pytest.raises(ValueError, match="BOT_MODE"):
        Settings()


def test_json_logs_carry_contextvars(capsys) -> None:
    from app import logging_setup

    logging_setup.configure_logging("INFO", "json")
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(update_id=42, ticket_id=7)
    log = logging_setup.get_logger("t")
    log.info("hello", extra_field="x")
    structlog.contextvars.clear_contextvars()

    out = capsys.readouterr().out
    record = json.loads(out.strip().splitlines()[-1])
    assert record["update_id"] == 42
    assert record["ticket_id"] == 7
    assert record["event"] == "hello"
    assert record["extra_field"] == "x"
    assert record["level"] == "info"
