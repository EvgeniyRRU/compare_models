"""Keep assignment data on closed tickets until reopen."""

import sqlalchemy as sa

from alembic import op

revision = "0003_closed_assignment"
down_revision = "0002_positive_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    constraints = {
        item["name"] for item in sa.inspect(op.get_bind()).get_check_constraints("tickets")
    }
    if "ticket_assignment_consistent" in constraints:
        op.drop_constraint("ticket_assignment_consistent", "tickets", type_="check")
    op.create_check_constraint(
        "ticket_assignment_consistent",
        "tickets",
        "(status = 'new' AND assignee_id IS NULL AND taken_at IS NULL) OR "
        "(status IN ('in_progress','waiting_info') AND assignee_id IS NOT NULL "
        "AND taken_at IS NOT NULL) OR "
        "(status IN ('resolved','rejected','duplicate') AND "
        "((assignee_id IS NULL AND taken_at IS NULL) OR "
        "(assignee_id IS NOT NULL AND taken_at IS NOT NULL)))",
    )


def downgrade() -> None:
    op.drop_constraint("ticket_assignment_consistent", "tickets", type_="check")
    op.create_check_constraint(
        "ticket_assignment_consistent",
        "tickets",
        "(status IN ('in_progress','waiting_info') AND assignee_id IS NOT NULL "
        "AND taken_at IS NOT NULL) OR "
        "(status NOT IN ('in_progress','waiting_info') AND assignee_id IS NULL "
        "AND taken_at IS NULL)",
    )
