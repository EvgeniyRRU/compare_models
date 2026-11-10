from types import SimpleNamespace

from sqlalchemy import func, select

from app.db.models import ProcessedUpdate
from app.main import process_update


async def test_failed_update_is_still_deduplicated(sessions):
    class Bot:
        calls = 0

        async def process_update(self, update):
            self.calls += 1
            raise RuntimeError("broken handler")

    bot = Bot()
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions=sessions, bot=bot, settings=SimpleNamespace(webhook_timeout_seconds=2)
        )
    )
    update = SimpleNamespace(update_id=23, effective_user=None)
    await process_update(app, update)
    await process_update(app, update)
    assert bot.calls == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ProcessedUpdate)) == 1
