"""dataset：已派发 DDM 命令的取消请求、原响应与停止核实结论。

Revision ID: 0073
Revises: 0072
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0073"
down_revision: str | None = "0072"


def upgrade() -> None:
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("cancel_requested_by", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("cancel_response", JSONB(), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("cancel_detail", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dataset_ddm_training_command", "cancel_detail")
    op.drop_column("dataset_ddm_training_command", "cancel_response")
    op.drop_column("dataset_ddm_training_command", "cancel_requested_at")
    op.drop_column("dataset_ddm_training_command", "cancel_requested_by")
