"""Require positive business and draft versions."""

import sqlalchemy as sa

from alembic import op

revision = "0002_positive_versions"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "ticket_version_positive" not in {
        item["name"] for item in inspector.get_check_constraints("tickets")
    }:
        op.create_check_constraint("ticket_version_positive", "tickets", "version >= 1")
    if "draft_version_positive" not in {
        item["name"] for item in inspector.get_check_constraints("ticket_drafts")
    }:
        op.create_check_constraint("draft_version_positive", "ticket_drafts", "version >= 1")


def downgrade() -> None:
    op.drop_constraint("draft_version_positive", "ticket_drafts", type_="check")
    op.drop_constraint("ticket_version_positive", "tickets", type_="check")
