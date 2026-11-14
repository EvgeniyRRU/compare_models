"""Tests for the PTB error handler (no database)."""

import logging
from types import SimpleNamespace
from typing import cast

import pytest
from telegram.ext import ContextTypes

from app.bot.errors import error_handler


async def test_error_handler_logs(caplog: pytest.LogCaptureFixture) -> None:
    error = ValueError("boom")
    context = cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace(error=error))

    with caplog.at_level(logging.ERROR, logger="app.bot.errors"):
        await error_handler(update=None, context=context)

    assert "Unhandled PTB error" in caplog.text
    assert caplog.records[0].exc_info is not None
    assert caplog.records[0].exc_info[1] is error
    assert "ValueError" in caplog.text


async def test_error_handler_noop_without_error(caplog: pytest.LogCaptureFixture) -> None:
    context = cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace(error=None))

    with caplog.at_level(logging.ERROR, logger="app.bot.errors"):
        await error_handler(update=None, context=context)

    assert caplog.records[0].exc_info is None
