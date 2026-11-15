"""Dedup service + guarded dispatch: reserve semantics and error persistence."""

from telegram import Update

from app.bot.dispatch import dispatch_update
from app.config import settings
from app.services import dedup

MSG = {
    "message_id": 1,
    "date": 1731600000,
    "chat": {"id": 7, "type": "private", "first_name": "T"},
    "from": {"id": 7, "is_bot": False, "first_name": "T"},
    "text": "/start",
}


def make_update(update_id: int) -> Update:
    update = Update.de_json({"update_id": update_id, "message": MSG}, None)
    assert update is not None
    return update


class Recorder:
    def __init__(self, fail_ids: set[int] | None = None) -> None:
        self.seen: list[int] = []
        self.fail_ids = fail_ids or set()

    async def process_update(self, update: Update) -> object:
        self.seen.append(update.update_id)
        if update.update_id in self.fail_ids:
            raise RuntimeError("handler exploded")


async def test_reserve_first_wins_second_loses(session_factory) -> None:
    assert await dedup.reserve(session_factory, 42) is True
    assert await dedup.reserve(session_factory, 42) is False
    assert await dedup.reserve(session_factory, 43) is True


async def test_dispatch_collapses_duplicate_delivery(session_factory) -> None:
    rec = Recorder()
    u = make_update(100)
    assert await dispatch_update(
        u, processor=rec, session_factory=session_factory, settings=settings
    ) == "ok"
    assert await dispatch_update(
        u, processor=rec, session_factory=session_factory, settings=settings
    ) == "duplicate"
    assert rec.seen == [100]


async def test_processing_error_keeps_reserve_row(session_factory) -> None:
    """A failed update must NOT be replayable: the reserve row survives the error."""
    rec = Recorder(fail_ids={7})
    result = await dispatch_update(
        make_update(7), processor=rec, session_factory=session_factory, settings=settings
    )
    assert result == "error"
    assert await dedup.reserve(session_factory, 7) is False
    again = await dispatch_update(
        make_update(7), processor=rec, session_factory=session_factory, settings=settings
    )
    assert again == "duplicate"
    assert rec.seen == [7]  # never re-executed


async def test_cleanup_removes_only_old_rows(session_factory) -> None:
    await dedup.reserve(session_factory, 1)
    await dedup.reserve(session_factory, 2)
    from sqlalchemy import text

    async with session_factory() as session, session.begin():
        await session.execute(
            text("UPDATE processed_updates SET created_at = now() - interval '49 hours' "
                 "WHERE update_id = 1")
        )
    removed = await dedup.cleanup(session_factory, 48)
    assert removed == 1
    assert await dedup.last_update_id(session_factory) == 2
