"""add account rotation fields

Revision ID: 20260520_0003
Revises: 20260520_0002
Create Date: 2026-05-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_0003"
down_revision: str | None = "20260520_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("telegram_accounts", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("telegram_accounts", sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index(op.f("ix_telegram_accounts_last_used_at"), "telegram_accounts", ["last_used_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_accounts_last_used_at"), table_name="telegram_accounts")
    op.drop_column("telegram_accounts", "usage_count")
    op.drop_column("telegram_accounts", "last_used_at")
