"""evidence：再切片请求与来源机冻结时间元数据。

`evidence_evidence` 增加来源机在入队事务里冻结的录像墙钟映射与核心必需跨度（旧登记为
NULL，表示截止未知、不可再切片，不用中心时钟反推）；`evidence_reclip_request` 保存新余量、
解析后的新窗口、发起人与委托命令 ID，不覆盖原证据，也不存媒体字节（ADR-0012）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: str | None = "0057"


def upgrade() -> None:
    op.add_column("evidence_evidence", sa.Column("wall_offset", sa.Float(), nullable=True))
    op.add_column("evidence_evidence", sa.Column("required_from", sa.Float(), nullable=True))
    op.add_column("evidence_evidence", sa.Column("required_to", sa.Float(), nullable=True))
    op.create_table(
        "evidence_reclip_request",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.String(length=255), nullable=False),
        sa.Column("generation", sa.String(length=128), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("anchor", sa.Float(), nullable=False),
        sa.Column("before_seconds", sa.Float(), nullable=False),
        sa.Column("after_seconds", sa.Float(), nullable=False),
        sa.Column("window_start", sa.Float(), nullable=False),
        sa.Column("window_end", sa.Float(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("command_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence_evidence.evidence_id"],
            name=op.f("fk_evidence_reclip_request_evidence_id_evidence_evidence"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("request_id", name=op.f("pk_evidence_reclip_request")),
    )
    op.create_index(
        op.f("ix_evidence_reclip_request_evidence_id"),
        "evidence_reclip_request",
        ["evidence_id"],
    )
    op.create_index(
        op.f("ix_evidence_reclip_request_host_id"), "evidence_reclip_request", ["host_id"]
    )
    op.create_index(
        op.f("ix_evidence_reclip_request_command_id"), "evidence_reclip_request", ["command_id"]
    )
    op.create_index(
        op.f("ix_evidence_reclip_request_status"), "evidence_reclip_request", ["status"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_evidence_reclip_request_status"), table_name="evidence_reclip_request")
    op.drop_index(
        op.f("ix_evidence_reclip_request_command_id"), table_name="evidence_reclip_request"
    )
    op.drop_index(op.f("ix_evidence_reclip_request_host_id"), table_name="evidence_reclip_request")
    op.drop_index(
        op.f("ix_evidence_reclip_request_evidence_id"), table_name="evidence_reclip_request"
    )
    op.drop_table("evidence_reclip_request")
    op.drop_column("evidence_evidence", "required_to")
    op.drop_column("evidence_evidence", "required_from")
    op.drop_column("evidence_evidence", "wall_offset")
