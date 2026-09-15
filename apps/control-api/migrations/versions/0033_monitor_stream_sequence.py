"""monitor：为两个 SSE 镜像流增加数据库高水位游标。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"


def upgrade() -> None:
    """为已有和新行分配不会倒退的流内序号。"""
    op.add_column(
        "monitor_reported_decision",
        sa.Column(
            "stream_sequence",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        op.f("uq_monitor_reported_decision_stream_sequence"),
        "monitor_reported_decision",
        ["stream_sequence"],
    )
    op.add_column(
        "monitor_reported_health",
        sa.Column(
            "stream_sequence",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        op.f("uq_monitor_reported_health_stream_sequence"),
        "monitor_reported_health",
        ["stream_sequence"],
    )


def downgrade() -> None:
    """回退流游标列及其唯一约束。"""
    op.drop_constraint(
        op.f("uq_monitor_reported_health_stream_sequence"),
        "monitor_reported_health",
        type_="unique",
    )
    op.drop_column("monitor_reported_health", "stream_sequence")
    op.drop_constraint(
        op.f("uq_monitor_reported_decision_stream_sequence"),
        "monitor_reported_decision",
        type_="unique",
    )
    op.drop_column("monitor_reported_decision", "stream_sequence")
