"""Track first /start for bootstrap administrators."""

import sqlalchemy as sa

from alembic import op

revision = "0004_user_started_at"
down_revision = "0003_closed_assignment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("users")}
    if "started_at" not in columns:
        op.add_column("users", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "started_at")
