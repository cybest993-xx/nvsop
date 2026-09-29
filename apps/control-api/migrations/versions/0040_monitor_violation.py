"""monitor：把上报判定随附的已锁存违规投影为独立可查询归档。

违规镜像不重新判定：它逐条保存推理机 `ReportedDecision.violations` 已经确认的事实，
按判定事件 id 与序号得到稳定事件身份，重复上报只保留首次归档。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0040"
down_revision: str | None = "0039"


def upgrade() -> None:
    op.create_table(
        "monitor_violation",
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("decision_event_id", sa.String(length=255), nullable=False),
        sa.Column("host_id", sa.String(length=128), nullable=False),
        sa.Column("station_id", sa.String(length=128), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_monitor_violation")),
    )
    op.create_index(
        op.f("ix_monitor_violation_decision_event_id"), "monitor_violation", ["decision_event_id"]
    )
    op.create_index(op.f("ix_monitor_violation_host_id"), "monitor_violation", ["host_id"])
    op.create_index(op.f("ix_monitor_violation_station_id"), "monitor_violation", ["station_id"])
    op.create_index(op.f("ix_monitor_violation_instance_id"), "monitor_violation", ["instance_id"])
    op.create_index(op.f("ix_monitor_violation_received_at"), "monitor_violation", ["received_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_monitor_violation_received_at"), table_name="monitor_violation")
    op.drop_index(op.f("ix_monitor_violation_instance_id"), table_name="monitor_violation")
    op.drop_index(op.f("ix_monitor_violation_station_id"), table_name="monitor_violation")
    op.drop_index(op.f("ix_monitor_violation_host_id"), table_name="monitor_violation")
    op.drop_index(op.f("ix_monitor_violation_decision_event_id"), table_name="monitor_violation")
    op.drop_table("monitor_violation")
