from typing import Protocol

from app.services.post_render import PostView


class MessageGone(Exception):
    """Пост удалён или недоступен для редактирования."""


class Publisher(Protocol):
    async def send(self, chat_id: int, view: PostView) -> int: ...

    async def edit(self, chat_id: int, message_id: int, view: PostView) -> None:
        """Бросает MessageGone, если пост нельзя отредактировать. 'not modified' — успех."""
        ...

    async def reply(self, chat_id: int, message_id: int | None, text: str) -> None: ...
