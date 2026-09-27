"""dataset：上传声明摘要改为可选，由中心从流式字节计算。

中心按 `control-plane.md` §训练数据集与标注 在流式落盘后计算大小、sha256 与媒体事实；
客户端声明摘要只是可选期望，浏览器不再为申请上传而整段读取文件。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"

# 回滚需要按当前语义补齐声明列，raw SQL 触及的表按门禁要求显式声明。
RAW_SQL_TABLES = frozenset({"dataset_member", "dataset_upload_attempt"})

# 仍未定稿的上传没有可回填的权威摘要。旧契约要求声明必须存在，故回填这个全零哨兵值，
# 让旧版校验按“声明与实测不一致”显式失败，而不是阻断回滚或删除数据。它不是任何真实摘要。
PLACEHOLDER_DECLARED_SHA256 = "0" * 64


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
    # 客户端不再必须声明摘要；恢复旧约束前先用中心登记的权威摘要补齐，否则 NOT NULL
    # 会在真实数据上失败、阻断应用回滚。
    op.execute(
        "UPDATE dataset_member SET declared_sha256 = actual_sha256 "
        "WHERE declared_sha256 IS NULL AND actual_sha256 IS NOT NULL"
    )
    op.execute(
        "UPDATE dataset_upload_attempt AS attempt SET declared_sha256 = member.actual_sha256 "
        "FROM dataset_member AS member "
        "WHERE attempt.member_id = member.id AND attempt.declared_sha256 IS NULL "
        "AND member.actual_sha256 IS NOT NULL"
    )
    # 仍未定稿的上传没有可回填的权威摘要。旧契约要求声明必须存在，这里回填全零占位值，
    # 让旧版校验按“声明与实测不一致”显式失败，而不是阻断回滚或删除数据。
    for table in ("dataset_member", "dataset_upload_attempt"):
        op.execute(
            sa.text(
                f"UPDATE {table} SET declared_sha256 = :placeholder WHERE declared_sha256 IS NULL"
            ).bindparams(placeholder=PLACEHOLDER_DECLARED_SHA256)
        )
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
