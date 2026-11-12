"""Create the initial support ticket schema."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

user_role = sa.Enum("support", "developer", "admin", name="user_role")
ticket_status = sa.Enum(
    "new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate",
    name="ticket_status",
)
ticket_priority = sa.Enum("critical", "high", "medium", "low", name="ticket_priority")
event_old_status = sa.Enum(
    "new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate",
    name="event_old_status",
)
event_new_status = sa.Enum(
    "new", "in_progress", "waiting_info", "resolved", "rejected", "duplicate",
    name="event_new_status",
)
draft_priority = sa.Enum("critical", "high", "medium", "low", name="draft_priority")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64)),
        sa.Column("full_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("role", user_role),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)

    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("author_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assignee_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("description", sa.String(length=3000), nullable=False),
        sa.Column("priority", ticket_priority, nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64)),
        sa.Column("status", ticket_status, nullable=False, server_default="new"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("taken_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("next_reminder_at", sa.DateTime(timezone=True)),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("post_synced_version", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("version >= 1", name="ticket_version_positive"),
        sa.CheckConstraint(
            "(status = 'new' AND assignee_id IS NULL AND taken_at IS NULL) OR "
            "(status IN ('in_progress','waiting_info') AND assignee_id IS NOT NULL "
            "AND taken_at IS NOT NULL) OR "
            "(status IN ('resolved','rejected','duplicate') AND "
            "((assignee_id IS NULL AND taken_at IS NULL) OR "
            "(assignee_id IS NOT NULL AND taken_at IS NOT NULL)))",
            name="ticket_assignment_consistent",
        ),
        sa.CheckConstraint(
            "(status IN ('resolved','rejected','duplicate')) = (closed_at IS NOT NULL)",
            name="ticket_closed_consistent",
        ),
    )
    op.create_index("ix_tickets_assignee_id", "tickets", ["assignee_id"])
    op.create_index("ix_tickets_pending_sync", "tickets", ["post_synced_version", "version"])
    op.create_index("ix_tickets_due_reminder", "tickets", ["next_reminder_at"])

    op.create_table(
        "ticket_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("old_status", event_old_status),
        sa.Column("new_status", event_new_status, nullable=False),
        sa.Column("old_assignee_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("new_assignee_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_ticket_events_ticket_id", "ticket_events", ["ticket_id"])

    op.create_table(
        "ticket_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("step", sa.String(length=24), nullable=False, server_default="description"),
        sa.Column("description", sa.String(length=3000)),
        sa.Column("priority", draft_priority),
        sa.Column("request_id", sa.String(length=64)),
        sa.Column("tenant_id", sa.String(length=64)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("prompt_message_id", sa.BigInteger()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("version >= 1", name="draft_version_positive"),
    )
    op.create_index("ix_ticket_drafts_user_id", "ticket_drafts", ["user_id"], unique=True)

    op.create_table(
        "processed_updates",
        sa.Column("update_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_processed_updates_created_at", "processed_updates", ["created_at"])


def downgrade() -> None:
    op.drop_table("processed_updates")
    op.drop_index("ix_ticket_drafts_user_id", table_name="ticket_drafts")
    op.drop_table("ticket_drafts")
    op.drop_index("ix_ticket_events_ticket_id", table_name="ticket_events")
    op.drop_table("ticket_events")
    op.drop_index("ix_tickets_due_reminder", table_name="tickets")
    op.drop_index("ix_tickets_pending_sync", table_name="tickets")
    op.drop_index("ix_tickets_assignee_id", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_table("users")
    for enum in (draft_priority, event_new_status, event_old_status, ticket_priority,
                 ticket_status, user_role):
        enum.drop(op.get_bind(), checkfirst=True)
