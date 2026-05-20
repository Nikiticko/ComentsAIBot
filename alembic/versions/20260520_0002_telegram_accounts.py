"""add telegram accounts

Revision ID: 20260520_0002
Revises: 20260520_0001
Create Date: 2026-05-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260520_0002"
down_revision: str | None = "20260520_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_name", sa.String(length=255), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=True),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_telegram_accounts_session_name"), "telegram_accounts", ["session_name"], unique=True)
    op.create_index(
        op.f("ix_telegram_accounts_telegram_user_id"),
        "telegram_accounts",
        ["telegram_user_id"],
        unique=True,
    )
    op.create_index(op.f("ix_telegram_accounts_username"), "telegram_accounts", ["username"], unique=False)
    op.create_index(op.f("ix_telegram_accounts_status"), "telegram_accounts", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_accounts_status"), table_name="telegram_accounts")
    op.drop_index(op.f("ix_telegram_accounts_username"), table_name="telegram_accounts")
    op.drop_index(op.f("ix_telegram_accounts_telegram_user_id"), table_name="telegram_accounts")
    op.drop_index(op.f("ix_telegram_accounts_session_name"), table_name="telegram_accounts")
    op.drop_table("telegram_accounts")
