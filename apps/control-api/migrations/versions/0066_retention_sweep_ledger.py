"""retention：回收执行账本——范围、删除/跳过计数与原因（#207）。

`retention_sweep` 每个调度周期最多一行：周期身份是主键，``running`` 崩溃残留被同周期重跑
覆盖，使 DB 删除失败与 worker 重启都可安全重跑；结果与失败原因分列，部分完成不会报成全量
成功。范围与结果都是 JSONB，由 retention 自己的模型编解码。

Revision ID: 0066
Revises: 0065
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0066"
down_revision: str | None = "0065"

_SWEEP_STATUS = ("running", "completed", "failed")


def upgrade() -> None:
    status = postgresql.ENUM(*_SWEEP_STATUS, name="retention_sweep_status", create_type=False)
    status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "retention_sweep",
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(*_SWEEP_STATUS, name="retention_sweep_status", create_type=False),
            nullable=False,
        ),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("failure_code", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("cycle_id", name=op.f("pk_retention_sweep")),
    )


def downgrade() -> None:
    op.drop_table("retention_sweep")
    postgresql.ENUM(*_SWEEP_STATUS, name="retention_sweep_status", create_type=False).drop(
        op.get_bind(), checkfirst=True
    )
