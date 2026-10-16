"""execution：正常交权请求与旧租约身份的持久状态机。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision: str | None = "0068"
branch_labels = None
depends_on = None

RAW_SQL_TABLES = frozenset({"execution_normal_handover"})


def upgrade() -> None:
    op.create_table(
        "execution_normal_handover",
        sa.Column("handover_id", sa.Uuid(), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("from_host_id", sa.Uuid(), nullable=False),
        sa.Column("to_host_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timed_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled', 'timed_out', 'failed')",
            name=op.f("ck_execution_normal_handover_status"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_execution_normal_handover_station_id_device_station"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["from_host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_execution_normal_handover_from_host_id_device_inference_host"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_execution_normal_handover_to_host_id_device_inference_host"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("handover_id", name=op.f("pk_execution_normal_handover")),
        sa.UniqueConstraint("command_id", name=op.f("uq_execution_normal_handover_command_id")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_execution_normal_handover_idempotency_key")
        ),
    )
    op.create_index(
        op.f("ix_execution_normal_handover_station_id"),
        "execution_normal_handover",
        ["station_id"],
    )
    op.create_index(
        op.f("ix_execution_normal_handover_from_host_id"),
        "execution_normal_handover",
        ["from_host_id"],
    )
    op.create_index(
        op.f("ix_execution_normal_handover_pending_station"),
        "execution_normal_handover",
        ["station_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_execution_normal_handover_pending_station"),
        table_name="execution_normal_handover",
    )
    op.drop_index(
        op.f("ix_execution_normal_handover_from_host_id"),
        table_name="execution_normal_handover",
    )
    op.drop_index(
        op.f("ix_execution_normal_handover_station_id"),
        table_name="execution_normal_handover",
    )
    op.drop_table("execution_normal_handover")
