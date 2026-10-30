import asyncio

from app.services.post_render import PostView
from app.services.publisher import MessageGone


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[tuple[int, PostView]] = []
        self.edits: list[tuple[int, PostView]] = []
        self.replies: list[tuple[int | None, str]] = []
        self.gone: set[int] = set()
        self.next_id = 100
        self.fail_next: Exception | None = None
        self.gate: asyncio.Event | None = None  # блокирует отправку до set()
        self.in_send = asyncio.Event()

    async def _maybe_block(self) -> None:
        self.in_send.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    async def send(self, chat_id: int, view: PostView) -> int:
        await self._maybe_block()
        self.next_id += 1
        self.sent.append((self.next_id, view))
        return self.next_id

    async def edit(self, chat_id: int, message_id: int, view: PostView) -> None:
        await self._maybe_block()
        if message_id in self.gone:
            raise MessageGone()
        self.edits.append((message_id, view))

    async def reply(self, chat_id: int, message_id: int | None, text: str) -> None:
        self.replies.append((message_id, text))
