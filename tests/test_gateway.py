"""Gateway helper tests: backoff schedule + BadRequest translation."""

import pytest
from telegram.error import BadRequest

from app.telegram_gateway import (
    MessageNotFound,
    RetryAfter,
    call_with_backoff,
    map_ptb_error,
)


async def test_backoff_honors_retry_after_with_fake_clock() -> None:
    sleeps: list[float] = []

    async def sleeper(seconds: float) -> None:
        sleeps.append(seconds)

    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RetryAfter(1.5)
        return "ok"

    result = await call_with_backoff(flaky, sleeper=sleeper)
    assert result == "ok"
    assert sleeps == [1.5, 1.5]
    assert attempts["n"] == 3


async def test_backoff_gives_up_after_max_attempts() -> None:
    async def sleeper(seconds: float) -> None:
        pass

    async def always_busy() -> str:
        raise RetryAfter(0.1)

    with pytest.raises(RetryAfter):
        await call_with_backoff(always_busy, max_attempts=3, sleeper=sleeper)


def test_map_not_modified_is_success() -> None:
    exc = BadRequest("Message is not modified")
    assert map_ptb_error(exc) is None


def test_map_deleted_message() -> None:
    exc = BadRequest("Message to edit not found")
    assert isinstance(map_ptb_error(exc), MessageNotFound)


def test_map_other_badrequest_passthrough() -> None:
    exc = BadRequest("REPLY_MARKUP_INVALID")
    assert map_ptb_error(exc) is exc
