"""monitor：持久化 edge 上报的 SOP 实例生命周期镜像。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0036"
down_revision: str | None = "0035"


def upgrade() -> None:
    op.create_table(
        "monitor_sop_instance",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("host_id", sa.String(length=128), nullable=False),
        sa.Column("station_id", sa.String(length=128), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("opened_at", sa.Float(), nullable=False),
        sa.Column("closed_at", sa.Float(), nullable=True),
        sa.Column("close_reason", sa.String(length=64), nullable=True),
        sa.Column("open_boundary_signal", sa.Text(), nullable=True),
        sa.Column("close_boundary_signal", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_monitor_sop_instance")),
    )
    op.create_index(op.f("ix_monitor_sop_instance_host_id"), "monitor_sop_instance", ["host_id"])
    op.create_index(
        op.f("ix_monitor_sop_instance_station_id"), "monitor_sop_instance", ["station_id"]
    )
    op.create_index(
        op.f("ix_monitor_sop_instance_received_at"), "monitor_sop_instance", ["received_at"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_monitor_sop_instance_received_at"), table_name="monitor_sop_instance")
    op.drop_index(op.f("ix_monitor_sop_instance_station_id"), table_name="monitor_sop_instance")
    op.drop_index(op.f("ix_monitor_sop_instance_host_id"), table_name="monitor_sop_instance")
    op.drop_table("monitor_sop_instance")
