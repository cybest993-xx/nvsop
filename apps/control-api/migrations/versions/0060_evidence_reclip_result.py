"""evidence：再切片成功追加新引用的结果身份。

`evidence_reclip_request` 增加 `result_evidence_id`：成功执行并把新引用追加到
`evidence_evidence` 后记录该新身份，使请求从 ``pending`` 转为 ``succeeded``；未成功为 NULL。
终态失败结案仍属 S039，不在此迁移或代码中写入。

Revision ID: 0060
Revises: 0059
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: str | None = "0059"


def upgrade() -> None:
    op.add_column(
        "evidence_reclip_request",
        sa.Column("result_evidence_id", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evidence_reclip_request", "result_evidence_id")
