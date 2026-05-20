"""initial schema

Revision ID: 20260520_0001
Revises:
Create Date: 2026-05-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_post_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_channels_username"), "channels", ["username"], unique=True)

    op.create_table(
        "logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=50), nullable=False),
        sa.Column("event", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(length=100), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_logs_event"), "logs", ["event"], unique=False)
    op.create_index(op.f("ix_logs_level"), "logs", ["level"], unique=False)

    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_settings_key"), "settings", ["key"], unique=True)

    op.create_table(
        "posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("telegram_post_id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("views_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("topic_analysis", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_posts_channel_id"), "posts", ["channel_id"], unique=False)
    op.create_index(op.f("ix_posts_status"), "posts", ["status"], unique=False)
    op.create_index(op.f("ix_posts_telegram_post_id"), "posts", ["telegram_post_id"], unique=False)

    op.create_table(
        "comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("telegram_comment_id", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_comments_post_id"), "comments", ["post_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_comments_post_id"), table_name="comments")
    op.drop_table("comments")
    op.drop_index(op.f("ix_posts_telegram_post_id"), table_name="posts")
    op.drop_index(op.f("ix_posts_status"), table_name="posts")
    op.drop_index(op.f("ix_posts_channel_id"), table_name="posts")
    op.drop_table("posts")
    op.drop_index(op.f("ix_settings_key"), table_name="settings")
    op.drop_table("settings")
    op.drop_index(op.f("ix_logs_level"), table_name="logs")
    op.drop_index(op.f("ix_logs_event"), table_name="logs")
    op.drop_table("logs")
    op.drop_index(op.f("ix_channels_username"), table_name="channels")
    op.drop_table("channels")
