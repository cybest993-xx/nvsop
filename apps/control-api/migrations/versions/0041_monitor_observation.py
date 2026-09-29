"""monitor：持久化 edge 上报的归一化观测镜像。

观测镜像按事件 id 幂等归档；中心只保存推理机产生时的实例、来源与模板/模型身份，
不重新判定，也不反写 edge。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0041"
down_revision: str | None = "0040"


def upgrade() -> None:
    op.create_table(
        "monitor_observation",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("trace_id", sa.String(length=255), nullable=False),
        sa.Column("host_id", sa.String(length=128), nullable=False),
        sa.Column("station_id", sa.String(length=128), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Float(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_monitor_observation")),
    )
    op.create_index(op.f("ix_monitor_observation_host_id"), "monitor_observation", ["host_id"])
    op.create_index(
        op.f("ix_monitor_observation_station_id"), "monitor_observation", ["station_id"]
    )
    op.create_index(
        op.f("ix_monitor_observation_instance_id"), "monitor_observation", ["instance_id"]
    )
    op.create_index(
        op.f("ix_monitor_observation_received_at"), "monitor_observation", ["received_at"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_monitor_observation_received_at"), table_name="monitor_observation")
    op.drop_index(op.f("ix_monitor_observation_instance_id"), table_name="monitor_observation")
    op.drop_index(op.f("ix_monitor_observation_station_id"), table_name="monitor_observation")
    op.drop_index(op.f("ix_monitor_observation_host_id"), table_name="monitor_observation")
    op.drop_table("monitor_observation")
