"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id"),
        sa.CheckConstraint("role IS NULL OR role IN ('support', 'developer', 'admin')", name="ck_users_role"),
    )
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="new", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("post_synced_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["assignee_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("length(description) BETWEEN 1 AND 3000", name="ck_tickets_description_len"),
        sa.CheckConstraint("priority IN ('critical', 'high', 'medium', 'low')", name="ck_tickets_priority"),
        sa.CheckConstraint("length(request_id) BETWEEN 1 AND 64", name="ck_tickets_request_id_len"),
        sa.CheckConstraint("tenant_id IS NULL OR length(tenant_id) BETWEEN 1 AND 64", name="ck_tickets_tenant_id_len"),
        sa.CheckConstraint("status IN ('new', 'in_progress', 'waiting_info', 'resolved', 'rejected', 'duplicate')", name="ck_tickets_status"),
        sa.CheckConstraint("version >= 1", name="ck_tickets_version"),
        sa.CheckConstraint("post_synced_version >= 0", name="ck_tickets_post_synced_version"),
        sa.CheckConstraint("status <> 'new' OR (assignee_id IS NULL AND taken_at IS NULL)", name="ck_tickets_new_unassigned"),
        sa.CheckConstraint("status NOT IN ('in_progress', 'waiting_info') OR assignee_id IS NOT NULL", name="ck_tickets_active_requires_assignee"),
        sa.CheckConstraint("status NOT IN ('in_progress', 'waiting_info') OR taken_at IS NOT NULL", name="ck_tickets_active_requires_taken_at"),
        sa.CheckConstraint("status IN ('resolved', 'rejected', 'duplicate') OR closed_at IS NULL", name="ck_tickets_closed_at_only_final"),
        sa.CheckConstraint("status NOT IN ('resolved', 'rejected', 'duplicate') OR closed_at IS NOT NULL", name="ck_tickets_closed_at_required_final"),
    )
    op.create_index("ix_tickets_status_next_reminder", "tickets", ["status", "next_reminder_at"])
    op.create_index("ix_tickets_author_id", "tickets", ["author_id"])
    op.create_index("ix_tickets_assignee_id", "tickets", ["assignee_id"])
    op.create_table(
        "ticket_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("old_status", sa.String(length=16), nullable=True),
        sa.Column("new_status", sa.String(length=16), nullable=True),
        sa.Column("old_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("new_assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["new_assignee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["old_assignee_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("event_type IN ('created', 'taken', 'status_changed', 'declined', 'reassigned', 'reopened')", name="ck_ticket_events_type"),
    )
    op.create_index("ix_ticket_events_ticket_id", "ticket_events", ["ticket_id"])
    op.create_table(
        "ticket_drafts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("step", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(length=16), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("prompt_message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        sa.CheckConstraint("step IN ('description', 'priority', 'request_id', 'tenant_id', 'confirm')", name="ck_drafts_step"),
        sa.CheckConstraint("version >= 1", name="ck_drafts_version"),
    )
    op.create_table(
        "processed_updates",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("update_id"),
    )


def downgrade() -> None:
    op.drop_table("processed_updates")
    op.drop_table("ticket_drafts")
    op.drop_index("ix_ticket_events_ticket_id", table_name="ticket_events")
    op.drop_table("ticket_events")
    op.drop_index("ix_tickets_assignee_id", table_name="tickets")
    op.drop_index("ix_tickets_author_id", table_name="tickets")
    op.drop_index("ix_tickets_status_next_reminder", table_name="tickets")
    op.drop_table("tickets")
    op.drop_table("users")
