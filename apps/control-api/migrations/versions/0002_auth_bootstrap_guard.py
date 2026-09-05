"""auth: serialize first-account bootstrap

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"


def upgrade() -> None:
    op.create_table(
        "auth_bootstrap_guard",
        sa.Column("singleton", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "singleton = 1",
            name=op.f("ck_auth_bootstrap_guard_singleton_is_one"),
        ),
        sa.PrimaryKeyConstraint(
            "singleton",
            name=op.f("pk_auth_bootstrap_guard"),
        ),
    )


def downgrade() -> None:
    op.drop_table("auth_bootstrap_guard")
