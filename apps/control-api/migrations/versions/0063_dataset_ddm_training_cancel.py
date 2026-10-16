"""dataset：未派发 DDM 命令的首次取消操作者与时刻。

Revision ID: 0063
Revises: 0062
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"


def upgrade() -> None:
    op.add_column(
        "dataset_ddm_training_command", sa.Column("cancelled_by", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dataset_ddm_training_command", "cancelled_at")
    op.drop_column("dataset_ddm_training_command", "cancelled_by")
