"""evidence：引用保护状态——在审复核占用、删除认领与播放占用（#206）。

`evidence_review_session` 保存"某位复核人正在复核这条证据"的持久占用（结论仍单独保存在只追加的
`evidence_review`）；`evidence_deletion_claim` 保存逐证据的删除认领，未结案的认领继续保护该引用；
`evidence_playback_lease` 保存短期播放占用。三张表都只引用本模块的 `evidence_evidence`。

历史 `evidence_evidence` 行的 `review_state_known` 写入 false：升级前的行无法证明"当时没有在审
复核"，而未知不得被解释为无人引用（§5.19）。新登记行由写入方一律写入真值。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"


def upgrade() -> None:
    op.add_column(
        "evidence_evidence",
        sa.Column("review_state_known", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "evidence_review_session",
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.String(length=255), nullable=False),
        sa.Column("generation", sa.String(length=128), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence_evidence.evidence_id"],
            name=op.f("fk_evidence_review_session_evidence_id_evidence_evidence"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("review_id", name=op.f("pk_evidence_review_session")),
    )
    op.create_index(
        op.f("ix_evidence_review_session_evidence_id"), "evidence_review_session", ["evidence_id"]
    )
    op.create_index(
        op.f("ix_evidence_review_session_reviewer_id"), "evidence_review_session", ["reviewer_id"]
    )
    op.create_table(
        "evidence_deletion_claim",
        sa.Column("claim_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.String(length=255), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column("effective_sha256", sa.String(length=64), nullable=False),
        sa.Column("default_revision", sa.Integer(), nullable=False),
        sa.Column("station_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        # 已授权阶段：中心在 verify 放行时在同一权威锁下写入；提交后持久，直到来源机终局反馈。
        # 普通（仅 claim）的未结案认领该列为 NULL，仍会因策略/配置变化返回既有 stale 原因。
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence_evidence.evidence_id"],
            name=op.f("fk_evidence_deletion_claim_evidence_id_evidence_evidence"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("claim_id", name=op.f("pk_evidence_deletion_claim")),
    )
    op.create_index(
        op.f("ix_evidence_deletion_claim_evidence_id"), "evidence_deletion_claim", ["evidence_id"]
    )
    op.create_index(
        op.f("ix_evidence_deletion_claim_host_id"), "evidence_deletion_claim", ["host_id"]
    )
    op.create_index(
        op.f("ix_evidence_deletion_claim_status"), "evidence_deletion_claim", ["status"]
    )
    # 权威变更守卫按“已授权且未结案”扫描；部分索引让检查与写入都只触碰活跃授权。
    op.create_index(
        op.f("ix_evidence_deletion_claim_authorized"),
        "evidence_deletion_claim",
        ["authorized_at"],
        postgresql_where=sa.text("status = 'pending' AND authorized_at IS NOT NULL"),
    )
    # 未结案认领按证据唯一：并发创建只有一个赢家，其余按冲突路径拒绝（§5.19 反 TOCTOU）。
    op.create_index(
        op.f("uq_evidence_deletion_claim_evidence_id"),
        "evidence_deletion_claim",
        ["evidence_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "evidence_playback_lease",
        sa.Column("evidence_id", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence_evidence.evidence_id"],
            name=op.f("fk_evidence_playback_lease_evidence_id_evidence_evidence"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("evidence_id", name=op.f("pk_evidence_playback_lease")),
    )
    # 历史行保持"复核状态未知"作为持久事实；默认值只用于这次回填，不留给后续插入。
    op.alter_column("evidence_evidence", "review_state_known", server_default=None)


def downgrade() -> None:
    op.drop_table("evidence_playback_lease")
    op.drop_index(
        op.f("uq_evidence_deletion_claim_evidence_id"),
        table_name="evidence_deletion_claim",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index(
        op.f("ix_evidence_deletion_claim_authorized"),
        table_name="evidence_deletion_claim",
        postgresql_where=sa.text("status = 'pending' AND authorized_at IS NOT NULL"),
    )
    op.drop_index(op.f("ix_evidence_deletion_claim_status"), table_name="evidence_deletion_claim")
    op.drop_index(op.f("ix_evidence_deletion_claim_host_id"), table_name="evidence_deletion_claim")
    op.drop_index(
        op.f("ix_evidence_deletion_claim_evidence_id"), table_name="evidence_deletion_claim"
    )
    op.drop_table("evidence_deletion_claim")
    op.drop_index(
        op.f("ix_evidence_review_session_reviewer_id"), table_name="evidence_review_session"
    )
    op.drop_index(
        op.f("ix_evidence_review_session_evidence_id"), table_name="evidence_review_session"
    )
    op.drop_table("evidence_review_session")
    op.drop_column("evidence_evidence", "review_state_known")
