from typing import Any

import pytest
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from support_bot.services.ports import Button, PostGoneError
from support_bot.telegram.publisher import TelegramPublisher, call_with_retry


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


def failing(*errors: BaseException, result: Any = "ok") -> Any:
    queue = list(errors)

    async def call() -> Any:
        if queue:
            raise queue.pop(0)
        return result

    return call


async def test_retry_after_respected() -> None:
    sleeps = Sleeps()
    out = await call_with_retry(
        failing(RetryAfter(7), TimedOut()),
        max_attempts=5,
        base_delay=1,
        max_delay=30,
        sleep=sleeps,
    )
    assert out == "ok"
    assert sleeps.calls[0] >= 7 and sleeps.calls[1] == 2  # backoff на второй попытке


async def test_gives_up_after_max_attempts() -> None:
    sleeps = Sleeps()
    with pytest.raises(TimedOut):
        await call_with_retry(
            failing(*(TimedOut() for _ in range(3))),
            max_attempts=3,
            base_delay=1,
            max_delay=30,
            sleep=sleeps,
        )
    assert sleeps.calls == [1, 2]


@pytest.mark.parametrize("error", [BadRequest("bad"), Forbidden("blocked")])
async def test_no_retry_for_client_errors(error: Exception) -> None:
    sleeps = Sleeps()
    with pytest.raises(type(error)):
        await call_with_retry(
            failing(error), max_attempts=5, base_delay=1, max_delay=30, sleep=sleeps
        )
    assert sleeps.calls == []


class EditBot:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> None:
        raise self.error


async def test_not_modified_is_success() -> None:
    pub = TelegramPublisher(EditBot(BadRequest("Bad Request: message is not modified")), 1)  # type: ignore[arg-type]
    await pub.edit_post(5, "text", ((Button("a", "t:tk:1:1"),),))


@pytest.mark.parametrize(
    "message", ["Message to edit not found", "Message can't be edited", "MESSAGE_ID_INVALID"]
)
async def test_gone_post(message: str) -> None:
    pub = TelegramPublisher(EditBot(BadRequest(message)), 1)  # type: ignore[arg-type]
    with pytest.raises(PostGoneError):
        await pub.edit_post(5, "text", ())
