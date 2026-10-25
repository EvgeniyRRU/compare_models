"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_role = postgresql.ENUM("support", "developer", "admin", name="user_role", create_type=False)
ticket_status = postgresql.ENUM(
    "new",
    "in_progress",
    "waiting_info",
    "resolved",
    "rejected",
    "duplicate",
    name="ticket_status",
    create_type=False,
)
ticket_priority = postgresql.ENUM(
    "critical", "high", "medium", "low", name="ticket_priority", create_type=False
)
ticket_event_type = postgresql.ENUM(
    "created",
    "taken",
    "status_changed",
    "released",
    "reassigned",
    "reopened",
    name="ticket_event_type",
    create_type=False,
)
draft_step = postgresql.ENUM(
    "description",
    "priority",
    "request_id",
    "tenant_id",
    "confirm",
    name="draft_step",
    create_type=False,
)
ENUMS = (user_role, ticket_status, ticket_priority, ticket_event_type, draft_step)
TABLES_WITH_UPDATED_AT = ("users", "tickets", "ticket_drafts")


def _timestamps() -> list[sa.Column[object]]:
    return [
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
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum in ENUMS:
        enum.create(bind, checkfirst=False)

    op.execute(
        """
        CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$
        """
    )

    op.create_table(
        "processed_updates",
        sa.Column("update_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("update_id", name="pk_processed_updates"),
    )
    op.create_index("ix_processed_updates_processed_at", "processed_updates", ["processed_at"])

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=256), nullable=True),
        sa.Column("last_name", sa.String(length=256), nullable=True),
        sa.Column("role", user_role, nullable=True),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("telegram_id", name="uq_users_telegram_id"),
    )

    op.create_table(
        "ticket_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("step", draft_step, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", ticket_priority, nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("prompt_message_id", sa.BigInteger(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("version >= 1", name="ck_ticket_drafts_version_positive"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_ticket_drafts_user_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_drafts"),
        sa.UniqueConstraint("user_id", name="uq_ticket_drafts_user_id"),
    )

    op.create_table(
        "tickets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.String(length=3000), nullable=False),
        sa.Column("priority", ticket_priority, nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("status", ticket_status, server_default="new", nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        *_timestamps(),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("post_synced_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)",
            name="ck_tickets_new_unassigned",
        ),
        sa.CheckConstraint(
            "status NOT IN ('in_progress', 'waiting_info') "
            "OR (assignee_id IS NOT NULL AND taken_at IS NOT NULL)",
            name="ck_tickets_active_assigned",
        ),
        sa.CheckConstraint(
            "(status IN ('resolved', 'rejected', 'duplicate')) = (closed_at IS NOT NULL)",
            name="ck_tickets_closed_at_iff_final",
        ),
        sa.CheckConstraint(
            "status = 'new' OR next_reminder_at IS NULL", name="ck_tickets_reminder_only_new"
        ),
        sa.CheckConstraint(
            "length(btrim(description)) > 0", name="ck_tickets_description_not_empty"
        ),
        sa.CheckConstraint("length(btrim(request_id)) > 0", name="ck_tickets_request_id_not_empty"),
        sa.CheckConstraint("version >= 1", name="ck_tickets_version_positive"),
        sa.ForeignKeyConstraint(["assignee_id"], ["users.id"], name="fk_tickets_assignee_id_users"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], name="fk_tickets_author_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_tickets"),
    )
    op.create_index(
        "ix_tickets_assignee_created",
        "tickets",
        ["assignee_id", sa.literal_column("created_at DESC")],
    )
    op.create_index(
        "ix_tickets_author_created", "tickets", ["author_id", sa.literal_column("created_at DESC")]
    )
    op.create_index(
        "ix_tickets_created_at",
        "tickets",
        [sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
    )
    op.create_index(
        "ix_tickets_due_reminders",
        "tickets",
        ["next_reminder_at"],
        postgresql_where=sa.text("status = 'new'"),
    )
    op.create_index(
        "ix_tickets_post_sync_pending",
        "tickets",
        ["id"],
        postgresql_where=sa.text("message_id IS NULL OR post_synced_version < version"),
    )

    op.create_table(
        "ticket_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", ticket_event_type, nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("old_status", ticket_status, nullable=True),
        sa.Column("new_status", ticket_status, nullable=False),
        sa.Column("old_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("new_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("version_after", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_ticket_events_actor_id_users"),
        sa.ForeignKeyConstraint(
            ["new_assignee_id"], ["users.id"], name="fk_ticket_events_new_assignee_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["old_assignee_id"], ["users.id"], name="fk_ticket_events_old_assignee_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.id"], name="fk_ticket_events_ticket_id_tickets"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ticket_events"),
    )
    op.create_index("ix_ticket_events_ticket_id_id", "ticket_events", ["ticket_id", "id"])

    for table in TABLES_WITH_UPDATED_AT:
        op.execute(
            f"CREATE TRIGGER trg_{table}_updated_at BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )


def downgrade() -> None:
    op.drop_table("ticket_events")
    op.drop_table("tickets")
    op.drop_table("ticket_drafts")
    op.drop_table("users")
    op.drop_table("processed_updates")
    op.execute("DROP FUNCTION set_updated_at()")
    bind = op.get_bind()
    for enum in reversed(ENUMS):
        enum.drop(bind, checkfirst=False)
