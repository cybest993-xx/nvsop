"""execution：中心工位物理执行权当前租约。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"


def upgrade() -> None:
    op.create_table(
        "execution_station_grant",
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("holder_host_id", sa.Uuid(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "lease_expires_at = renewed_at + INTERVAL '7 days'",
            name=op.f("ck_execution_station_grant_seven_day_ttl"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_execution_station_grant_station_id_device_station"),
        ),
        sa.ForeignKeyConstraint(
            ["holder_host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_execution_station_grant_holder_host_id_device_inference_host"),
        ),
        sa.PrimaryKeyConstraint("station_id", name=op.f("pk_execution_station_grant")),
        sa.UniqueConstraint("grant_id", name=op.f("uq_execution_station_grant_grant_id")),
    )
    op.create_index(
        op.f("ix_execution_station_grant_holder_host_id"),
        "execution_station_grant",
        ["holder_host_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_execution_station_grant_holder_host_id"),
        table_name="execution_station_grant",
    )
    op.drop_table("execution_station_grant")
