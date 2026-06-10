"""add channel status and stats

Revision ID: 20260610_0007
Revises: 20260608_0006
Create Date: 2026-06-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260610_0007"
down_revision: str | None = "20260608_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channels",
        sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
    )
    op.add_column(
        "channels",
        sa.Column("checks_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "channels",
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "channels",
        sa.Column("fail_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "channels",
        sa.Column("posts_checked_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "channels",
        sa.Column("comments_closed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "channels",
        sa.Column("too_short_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("channels", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "channels",
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "channels",
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_channels_status"), "channels", ["status"], unique=False)
    op.create_index(
        op.f("ix_channels_last_checked_at"),
        "channels",
        ["last_checked_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_channels_cooldown_until"),
        "channels",
        ["cooldown_until"],
        unique=False,
    )
    op.execute("UPDATE channels SET status = 'ignored' WHERE is_active = 0")


def downgrade() -> None:
    op.drop_index(op.f("ix_channels_cooldown_until"), table_name="channels")
    op.drop_index(op.f("ix_channels_last_checked_at"), table_name="channels")
    op.drop_index(op.f("ix_channels_status"), table_name="channels")
    op.drop_column("channels", "cooldown_until")
    op.drop_column("channels", "last_checked_at")
    op.drop_column("channels", "last_error")
    op.drop_column("channels", "too_short_count")
    op.drop_column("channels", "comments_closed_count")
    op.drop_column("channels", "posts_checked_count")
    op.drop_column("channels", "fail_count")
    op.drop_column("channels", "success_count")
    op.drop_column("channels", "checks_count")
    op.drop_column("channels", "status")
