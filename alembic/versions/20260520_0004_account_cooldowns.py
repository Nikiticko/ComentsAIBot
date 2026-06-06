"""add telegram account cooldown fields

Revision ID: 20260520_0004
Revises: 20260520_0003
Create Date: 2026-05-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_0004"
down_revision: str | None = "20260520_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telegram_accounts",
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("telegram_accounts", sa.Column("cooldown_reason", sa.Text(), nullable=True))
    op.add_column(
        "telegram_accounts",
        sa.Column("cooldown_source", sa.String(length=100), nullable=True),
    )
    op.add_column("telegram_accounts", sa.Column("flood_wait_seconds", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_telegram_accounts_cooldown_until"),
        "telegram_accounts",
        ["cooldown_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_accounts_cooldown_until"), table_name="telegram_accounts")
    op.drop_column("telegram_accounts", "flood_wait_seconds")
    op.drop_column("telegram_accounts", "cooldown_source")
    op.drop_column("telegram_accounts", "cooldown_reason")
    op.drop_column("telegram_accounts", "cooldown_until")
