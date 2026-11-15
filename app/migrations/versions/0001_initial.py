"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS: dict[str, tuple[str, ...]] = {
    "role": ("support", "developer", "admin"),
    "priority": ("critical", "high", "medium", "low"),
    "ticket_status": (
        "new",
        "in_progress",
        "waiting_info",
        "resolved",
        "rejected",
        "duplicate",
    ),
    "draft_step": ("description", "priority", "request_id", "tenant_id", "confirm"),
    "event_type": ("created", "taken", "status_changed", "abandoned", "reassigned", "reopened"),
}


def upgrade() -> None:
    for name, values in ENUMS.items():
        op.execute(f"CREATE TYPE {name} AS ENUM ({', '.join(repr(v) for v in values)})")

    # Single consistent mechanism for updated_at: DB trigger on every table that has it.
    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$;
        """
    )

    op.create_table(
        "users",
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("full_name", sa.String(length=256), server_default="", nullable=False),
        sa.Column("role", sa.Enum(name="role", create_type=False), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("telegram_id"),
    )

    op.create_table(
        "tickets",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.Enum(name="priority", create_type=False), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.Enum(name="ticket_status", create_type=False),
            server_default="new",
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("post_synced_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)",
            name="ck_new_no_assignee",
        ),
        sa.CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') "
            "OR (assignee_id IS NOT NULL AND taken_at IS NOT NULL)",
            name="ck_active_has_assignee",
        ),
        sa.CheckConstraint(
            "(closed_at IS NOT NULL) = (status IN ('resolved', 'rejected', 'duplicate'))",
            name="ck_closed_iff_final",
        ),
        sa.ForeignKeyConstraint(["author_id"], ["users.telegram_id"]),
        sa.ForeignKeyConstraint(["assignee_id"], ["users.telegram_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tickets_author_id"), "tickets", ["author_id"])
    op.create_index(op.f("ix_tickets_assignee_id"), "tickets", ["assignee_id"])
    op.execute("CREATE INDEX ix_tickets_new_due ON tickets (next_reminder_at) WHERE status = 'new'")
    op.execute(
        "CREATE INDEX ix_tickets_needs_post_sync ON tickets (id) "
        "WHERE message_id IS NULL OR post_synced_version < version"
    )

    op.create_table(
        "ticket_events",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.Enum(name="event_type", create_type=False), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("old_status", sa.Enum(name="ticket_status", create_type=False), nullable=True),
        sa.Column("new_status", sa.Enum(name="ticket_status", create_type=False), nullable=True),
        sa.Column("old_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("new_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("version_after", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.telegram_id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ticket_events_ticket_id"), "ticket_events", ["ticket_id"])

    op.create_table(
        "ticket_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("step", sa.Enum(name="draft_step", create_type=False), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.Enum(name="priority", create_type=False), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("prompt_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.telegram_id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_ticket_drafts_user"),
    )

    op.create_table(
        "processed_updates",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("update_id"),
    )

    for table in ("users", "tickets", "ticket_drafts"):
        op.execute(
            f"CREATE TRIGGER trg_set_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    for table in ("users", "tickets", "ticket_drafts"):
        op.execute(f"DROP TRIGGER trg_set_updated_at ON {table}")
    op.drop_table("processed_updates")
    op.drop_table("ticket_drafts")
    op.drop_index(op.f("ix_ticket_events_ticket_id"), table_name="ticket_events")
    op.drop_table("ticket_events")
    op.drop_index("ix_tickets_needs_post_sync", table_name="tickets")
    op.drop_index("ix_tickets_new_due", table_name="tickets")
    op.drop_index(op.f("ix_tickets_assignee_id"), table_name="tickets")
    op.drop_index(op.f("ix_tickets_author_id"), table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("users")
    op.execute("DROP FUNCTION set_updated_at()")
    for name in ENUMS:
        op.execute(f"DROP TYPE {name}")
