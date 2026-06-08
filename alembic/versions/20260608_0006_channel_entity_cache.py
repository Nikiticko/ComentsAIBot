"""add channel entity cache

Revision ID: 20260608_0006
Revises: 20260607_0005
Create Date: 2026-06-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260608_0006"
down_revision: str | None = "20260607_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("telegram_channel_id", sa.BigInteger(), nullable=True))
    op.add_column("channels", sa.Column("telegram_access_hash", sa.BigInteger(), nullable=True))
    op.add_column("channels", sa.Column("entity_resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("channels", sa.Column("entity_error", sa.Text(), nullable=True))
    op.create_index(
        op.f("ix_channels_telegram_channel_id"),
        "channels",
        ["telegram_channel_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_channels_telegram_channel_id"), table_name="channels")
    op.drop_column("channels", "entity_error")
    op.drop_column("channels", "entity_resolved_at")
    op.drop_column("channels", "telegram_access_hash")
    op.drop_column("channels", "telegram_channel_id")
