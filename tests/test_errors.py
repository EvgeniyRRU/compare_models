"""Tests for the PTB error handler (no database)."""

import logging
from types import SimpleNamespace
from typing import cast

import pytest
from telegram.ext import ContextTypes

from app.bot.errors import error_handler


async def test_error_handler_logs(caplog: pytest.LogCaptureFixture) -> None:
    context = cast(ContextTypes.DEFAULT_TYPE, SimpleNamespace(error=ValueError("boom")))

    with caplog.at_level(logging.ERROR, logger="app.bot.errors"):
        await error_handler(update=None, context=context)

    assert "Unhandled PTB error" in caplog.text
