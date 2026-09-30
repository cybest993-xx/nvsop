"""evidence：登记推理机本机持有的证据引用。

中心只登记稳定证据 ID、来源推理机、锚点/窗口、代次、sha256、大小和受控本机引用；媒体字节
留在来源推理机，故这里没有对象存储键、也没有“媒体已复制到中心”的列（ADR-0012）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"


def upgrade() -> None:
    op.create_table(
        "evidence_evidence",
        sa.Column("evidence_id", sa.String(length=255), nullable=False),
        sa.Column("host_id", sa.String(length=128), nullable=False),
        sa.Column("station_id", sa.String(length=128), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("violation_id", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("anchor", sa.Float(), nullable=False),
        sa.Column("window_start", sa.Float(), nullable=False),
        sa.Column("window_end", sa.Float(), nullable=False),
        sa.Column("generation", sa.String(length=128), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("reference", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("evidence_id", name=op.f("pk_evidence_evidence")),
    )
    op.create_index(op.f("ix_evidence_evidence_host_id"), "evidence_evidence", ["host_id"])
    op.create_index(op.f("ix_evidence_evidence_station_id"), "evidence_evidence", ["station_id"])
    op.create_index(op.f("ix_evidence_evidence_instance_id"), "evidence_evidence", ["instance_id"])
    op.create_index(op.f("ix_evidence_evidence_status"), "evidence_evidence", ["status"])
    op.create_index(op.f("ix_evidence_evidence_received_at"), "evidence_evidence", ["received_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_evidence_evidence_received_at"), table_name="evidence_evidence")
    op.drop_index(op.f("ix_evidence_evidence_status"), table_name="evidence_evidence")
    op.drop_index(op.f("ix_evidence_evidence_instance_id"), table_name="evidence_evidence")
    op.drop_index(op.f("ix_evidence_evidence_station_id"), table_name="evidence_evidence")
    op.drop_index(op.f("ix_evidence_evidence_host_id"), table_name="evidence_evidence")
    op.drop_table("evidence_evidence")
