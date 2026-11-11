import json

import pytest
import structlog

from support_bot.logging import configure_logging, tagged


def test_json_logs_include_tags_bound_in_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", fmt="json")
    log = structlog.get_logger()

    with tagged(update_id=42, telegram_user_id=7):
        log.info("handled", component="bot")

    record = json.loads(capsys.readouterr().out.strip())
    assert record["event"] == "handled"
    assert record["update_id"] == 42
    assert record["telegram_user_id"] == 7
    assert record["component"] == "bot"
    assert record["level"] == "info"


def test_tags_are_removed_after_context_exits(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", fmt="json")
    log = structlog.get_logger()

    with tagged(ticket_id=1):
        pass
    log.info("after")

    record = json.loads(capsys.readouterr().out.strip())
    assert "ticket_id" not in record


def test_levels_below_threshold_are_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="WARNING", fmt="json")

    structlog.get_logger().info("quiet")

    assert capsys.readouterr().out == ""
