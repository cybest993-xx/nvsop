"""evidence：登记人工复核结论。

复核结论只追加、不改写机器判定；这里只引用本模块证据表，不引用 auth/monitor 表，复核人
身份按仓库约定存为合法 UUID（与 `created_by` 等归属列一致）。复核人显示名是提交时快照。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"


def upgrade() -> None:
    op.create_table(
        "evidence_review",
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.String(length=255), nullable=False),
        sa.Column("generation", sa.String(length=128), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("reviewer_name", sa.String(length=255), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conclusion", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_id"],
            ["evidence_evidence.evidence_id"],
            name=op.f("fk_evidence_review_evidence_id_evidence_evidence"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("review_id", name=op.f("pk_evidence_review")),
    )
    op.create_index(op.f("ix_evidence_review_evidence_id"), "evidence_review", ["evidence_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_evidence_review_evidence_id"), table_name="evidence_review")
    op.drop_table("evidence_review")
