"""Tests that the Alembic migration produces the expected schema."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

EXPECTED_TABLES = {
    "users",
    "tickets",
    "ticket_events",
    "ticket_drafts",
    "processed_updates",
}


async def test_migration_creates_all_tables(engine: AsyncEngine) -> None:
    def table_names(sync_conn: Connection) -> list[str]:
        return inspect(sync_conn).get_table_names()

    async with engine.connect() as conn:
        names = await conn.run_sync(table_names)

    assert set(names) >= EXPECTED_TABLES


async def test_migration_records_single_head_revision(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        revisions = result.scalars().all()

    assert len(revisions) == 1
