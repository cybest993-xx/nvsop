"""monitor：推理机上报判定与健康的幂等镜像。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0031"
down_revision: str | None = "0030"


def upgrade() -> None:
    op.create_table(
        "monitor_reported_decision",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("trace_id", sa.String(length=255), nullable=False),
        sa.Column("host_id", sa.String(length=128), nullable=False),
        sa.Column("station_id", sa.String(length=128), nullable=False),
        sa.Column("backend_id", sa.String(length=128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_monitor_reported_decision")),
    )
    op.create_index(
        op.f("ix_monitor_reported_decision_host_id"),
        "monitor_reported_decision",
        ["host_id"],
    )
    op.create_index(
        op.f("ix_monitor_reported_decision_station_id"),
        "monitor_reported_decision",
        ["station_id"],
    )
    op.create_index(
        op.f("ix_monitor_reported_decision_backend_id"),
        "monitor_reported_decision",
        ["backend_id"],
    )
    op.create_index(
        op.f("ix_monitor_reported_decision_received_at"),
        "monitor_reported_decision",
        ["received_at"],
    )

    op.create_table(
        "monitor_reported_health",
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("trace_id", sa.String(length=255), nullable=False),
        sa.Column("host_id", sa.String(length=128), nullable=False),
        sa.Column("station_id", sa.String(length=128), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_monitor_reported_health")),
    )
    op.create_index(
        op.f("ix_monitor_reported_health_host_id"),
        "monitor_reported_health",
        ["host_id"],
    )
    op.create_index(
        op.f("ix_monitor_reported_health_station_id"),
        "monitor_reported_health",
        ["station_id"],
    )
    op.create_index(
        op.f("ix_monitor_reported_health_received_at"),
        "monitor_reported_health",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_monitor_reported_health_received_at"), table_name="monitor_reported_health"
    )
    op.drop_index(
        op.f("ix_monitor_reported_health_station_id"), table_name="monitor_reported_health"
    )
    op.drop_index(op.f("ix_monitor_reported_health_host_id"), table_name="monitor_reported_health")
    op.drop_table("monitor_reported_health")
    op.drop_index(
        op.f("ix_monitor_reported_decision_received_at"), table_name="monitor_reported_decision"
    )
    op.drop_index(
        op.f("ix_monitor_reported_decision_backend_id"), table_name="monitor_reported_decision"
    )
    op.drop_index(
        op.f("ix_monitor_reported_decision_station_id"), table_name="monitor_reported_decision"
    )
    op.drop_index(
        op.f("ix_monitor_reported_decision_host_id"), table_name="monitor_reported_decision"
    )
    op.drop_table("monitor_reported_decision")
