"""execution：强制改绑双人确认记录。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"


def upgrade() -> None:
    op.create_table(
        "execution_handover",
        sa.Column("handover_id", sa.Uuid(), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("from_host_id", sa.Uuid(), nullable=False),
        sa.Column("to_host_id", sa.Uuid(), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("operator_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operator_risk_shown", sa.Boolean(), nullable=False),
        sa.Column("second_operator_id", sa.Uuid(), nullable=True),
        sa.Column("second_confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("second_risk_shown", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "(second_operator_id IS NULL) = (second_confirmed_at IS NULL) "
            "AND (second_operator_id IS NULL) = (second_risk_shown IS NULL)",
            name=op.f("ck_execution_handover_second_confirmation_together"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_execution_handover_station_id_device_station"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_execution_handover_from_host_id_device_inference_host"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_execution_handover_to_host_id_device_inference_host"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("handover_id", name=op.f("pk_execution_handover")),
    )
    op.create_index(op.f("ix_execution_handover_station_id"), "execution_handover", ["station_id"])
    op.create_index(
        op.f("ix_execution_handover_from_host_id"), "execution_handover", ["from_host_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_execution_handover_from_host_id"), table_name="execution_handover")
    op.drop_index(op.f("ix_execution_handover_station_id"), table_name="execution_handover")
    op.drop_table("execution_handover")
