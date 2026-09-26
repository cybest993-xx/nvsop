"""dataset：上传声明摘要改为可选，由中心从流式字节计算。

中心按 `control-plane.md` §训练数据集与标注 在流式落盘后计算大小、sha256 与媒体事实；
客户端声明摘要只是可选期望，浏览器不再为申请上传而整段读取文件。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"


def upgrade() -> None:
    op.alter_column(
        "dataset_member",
        "declared_sha256",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.alter_column(
        "dataset_upload_attempt",
        "declared_sha256",
        existing_type=sa.String(length=64),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "dataset_upload_attempt",
        "declared_sha256",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.alter_column(
        "dataset_member",
        "declared_sha256",
        existing_type=sa.String(length=64),
        nullable=False,
    )
