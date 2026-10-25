"""Интерфейсы внешних систем для бизнес-слоя. Конкретная реализация — Telegram."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Button:
    text: str
    callback_data: str


type Keyboard = tuple[tuple[Button, ...], ...]


class PostGoneError(Exception):
    """Пост удалён или недоступен для редактирования."""


class PostPublisher(Protocol):
    async def send_post(self, text: str, keyboard: Keyboard) -> int:
        """Опубликовать новый пост, вернуть его message_id."""
        ...

    async def edit_post(self, message_id: int, text: str, keyboard: Keyboard) -> None:
        """Отредактировать пост. «message is not modified» считается успехом.

        Raises:
            PostGoneError: пост удалён или его нельзя редактировать.
        """
        ...

    async def send_reminder(self, text: str, reply_to_message_id: int | None) -> None:
        """Отправить напоминание (одна попытка на срок, без ретраев на уровне вызывающего)."""
        ...
