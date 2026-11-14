"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_role = postgresql.ENUM(
    "support",
    "developer",
    "admin",
    name="user_role",
    create_type=False,
)
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
    "declined",
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


def upgrade() -> None:
    bind = op.get_bind()
    for enum_type in (
        user_role,
        ticket_status,
        ticket_priority,
        ticket_event_type,
        draft_step,
    ):
        enum_type.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("role", user_role, nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("telegram_id", name=op.f("uq_users_telegram_id")),
    )
    op.create_index(op.f("ix_users_role"), "users", ["role"], unique=False)

    op.create_table(
        "tickets",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", ticket_priority, nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=True),
        sa.Column("status", ticket_status, server_default=sa.text("'new'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("post_synced_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 3000",
            name=op.f("ck_tickets_description_length"),
        ),
        sa.CheckConstraint(
            "char_length(request_id) BETWEEN 1 AND 64",
            name=op.f("ck_tickets_request_id_length"),
        ),
        sa.CheckConstraint(
            "tenant_id IS NULL OR char_length(tenant_id) BETWEEN 1 AND 64",
            name=op.f("ck_tickets_tenant_id_length"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_tickets_version_positive")),
        sa.CheckConstraint(
            "post_synced_version >= 0",
            name=op.f("ck_tickets_post_synced_version_non_negative"),
        ),
        sa.CheckConstraint(
            "(status = 'new' AND assignee_id IS NULL AND taken_at IS NULL) "
            "OR (status <> 'new' AND assignee_id IS NOT NULL AND taken_at IS NOT NULL)",
            name=op.f("ck_tickets_assignee_matches_status"),
        ),
        sa.CheckConstraint(
            "(status IN ('resolved', 'rejected', 'duplicate')) = (closed_at IS NOT NULL)",
            name=op.f("ck_tickets_closed_at_matches_status"),
        ),
        sa.CheckConstraint(
            "(status = 'new') = (next_reminder_at IS NOT NULL)",
            name=op.f("ck_tickets_reminder_matches_status"),
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name=op.f("fk_tickets_author_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id"],
            ["users.id"],
            name=op.f("fk_tickets_assignee_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tickets")),
    )
    op.create_index(
        "ix_tickets_assignee_created_at", "tickets", ["assignee_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_tickets_author_created_at", "tickets", ["author_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_tickets_post_sync",
        "tickets",
        ["id"],
        unique=False,
        postgresql_where=sa.text("message_id IS NULL OR post_synced_version < version"),
    )
    op.create_index(
        "ix_tickets_status_created_at", "tickets", ["status", "created_at"], unique=False
    )
    op.create_index(
        "ix_tickets_due_reminder",
        "tickets",
        ["next_reminder_at"],
        unique=False,
        postgresql_where=sa.text("status = 'new'"),
    )

    op.create_table(
        "ticket_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("ticket_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", ticket_event_type, nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("old_status", ticket_status, nullable=True),
        sa.Column("new_status", ticket_status, nullable=True),
        sa.Column("old_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("new_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["users.id"],
            name=op.f("fk_ticket_events_actor_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["new_assignee_id"],
            ["users.id"],
            name=op.f("fk_ticket_events_new_assignee_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["old_assignee_id"],
            ["users.id"],
            name=op.f("fk_ticket_events_old_assignee_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["tickets.id"],
            name=op.f("fk_ticket_events_ticket_id_tickets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_events")),
    )
    op.create_index(
        "ix_ticket_events_ticket_created_at",
        "ticket_events",
        ["ticket_id", "created_at"],
        unique=False,
    )

    op.create_table(
        "ticket_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("step", draft_step, server_default="description", nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", ticket_priority, nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("tenant_id", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_ticket_drafts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_drafts")),
        sa.UniqueConstraint("user_id", name=op.f("uq_ticket_drafts_user_id")),
    )
    op.create_index(
        op.f("ix_ticket_drafts_updated_at"), "ticket_drafts", ["updated_at"], unique=False
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
        sa.PrimaryKeyConstraint("update_id", name=op.f("pk_processed_updates")),
    )
    op.create_index(
        op.f("ix_processed_updates_created_at"),
        "processed_updates",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_processed_updates_created_at"), table_name="processed_updates")
    op.drop_table("processed_updates")
    op.drop_index(op.f("ix_ticket_drafts_updated_at"), table_name="ticket_drafts")
    op.drop_table("ticket_drafts")
    op.drop_index("ix_ticket_events_ticket_created_at", table_name="ticket_events")
    op.drop_table("ticket_events")
    op.drop_index("ix_tickets_due_reminder", table_name="tickets")
    op.drop_index("ix_tickets_status_created_at", table_name="tickets")
    op.drop_index("ix_tickets_post_sync", table_name="tickets")
    op.drop_index("ix_tickets_author_created_at", table_name="tickets")
    op.drop_index("ix_tickets_assignee_created_at", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index(op.f("ix_users_role"), table_name="users")
    op.drop_table("users")

    bind = op.get_bind()
    for enum_type in (
        draft_step,
        ticket_event_type,
        ticket_priority,
        ticket_status,
        user_role,
    ):
        enum_type.drop(bind, checkfirst=True)
