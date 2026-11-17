# tests/integration/test_updates_dedup.py
from sqlalchemy import text

from app.services.updates import purge_processed_updates, register_update


async def test_register_once(db):
    assert await register_update(db, 100) is True
    assert await register_update(db, 100) is False
    assert await register_update(db, 101) is True


async def test_error_does_not_rollback_dedup(db):
    """Ошибка бизнес-обработки не откатывает запись о дедупликации."""
    assert await register_update(db, 200) is True
    try:
        async with db.session_factory() as session, session.begin():
            await session.execute(text("SELECT 1/0"))
    except Exception:
        pass
    assert await register_update(db, 200) is False  # запись жива


async def test_purge_old_updates(db):
    await register_update(db, 300)
    async with db.session_factory() as s, s.begin():
        await s.execute(text("UPDATE processed_updates SET processed_at = now() - interval '49 hours'"))
    await purge_processed_updates(db)
    async with db.session_factory() as s:
        n = (await s.execute(text("SELECT count(*) FROM processed_updates"))).scalar_one()
    assert n == 0
