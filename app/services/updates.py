"""Дедупликация апдейтов (SPEC «FastAPI»).

Запись коммитится отдельной короткой транзакцией ДО бизнес-обработки,
поэтому ошибка обработки её не откатывает. Не более одной попытки
обработки каждого апдейта.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CursorResult, text

from app.db import DB


async def register_update(db: DB, update_id: int) -> bool:
    """True, если апдейт обрабатывается впервые."""
    async with db.session_factory() as session, session.begin():
        result = cast(
            CursorResult[()],
            await session.execute(
                text(
                    "INSERT INTO processed_updates (update_id) VALUES (:id) "
                    "ON CONFLICT (update_id) DO NOTHING"
                ),
                {"id": update_id},
            ),
        )
        return (result.rowcount or 0) == 1


async def purge_processed_updates(db: DB, older_than_hours: int = 48) -> None:
    async with db.session_factory() as session, session.begin():
        await session.execute(
            text(
                "DELETE FROM processed_updates WHERE processed_at < now() - "
                "make_interval(hours => :h)"
            ),
            {"h": older_than_hours},
        )
