"""add telegram account mailing flag

Revision ID: 20260607_0005
Revises: 20260520_0004
Create Date: 2026-06-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260607_0005"
down_revision: str | None = "20260520_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telegram_accounts",
        sa.Column("is_mailing_enabled", sa.Boolean(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("telegram_accounts", "is_mailing_enabled")
