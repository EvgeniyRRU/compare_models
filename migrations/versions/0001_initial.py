"""Initial immutable schema: ticketing, drafts, audit, and deduplication.

Revision ID: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # The initial schema is frozen SQL, independent of future ORM model changes.
    from pathlib import Path

    schema = Path(__file__).with_name("0001_schema.sql").read_text()
    for statement in schema.split("-- statement --"):
        if statement.strip():
            op.execute(sa.text(statement))


def downgrade():
    for table in ("processed_updates", "ticket_drafts", "ticket_events", "tickets", "users"):
        op.drop_table(table)
    op.execute("DROP FUNCTION support_set_updated_at()")
