"""dataset：把对象存储代次命名的列改成定稿文件身份。

`control-plane.md` §训练数据集与标注 要求公开/业务契约不再携带 S3 特有的对象代次语义；
这些列在 #350 之后保存的都是中心本地定稿文件键，故按文件身份命名。成员列与 `object_key`
完全重复，直接删除。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"


def upgrade() -> None:
    op.drop_column("dataset_member", "object_version_id")
    op.alter_column(
        "dataset_upload_attempt",
        "object_version_id",
        new_column_name="final_object_key",
        existing_type=sa.String(length=255),
        existing_nullable=True,
    )
    op.alter_column(
        "dataset_annotation_submission",
        "source_object_version_id",
        new_column_name="source_object_key",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        "dataset_annotation_context",
        "source_object_version_id",
        new_column_name="source_object_key",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "dataset_annotation_context",
        "source_object_key",
        new_column_name="source_object_version_id",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        "dataset_annotation_submission",
        "source_object_key",
        new_column_name="source_object_version_id",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        "dataset_upload_attempt",
        "final_object_key",
        new_column_name="object_version_id",
        existing_type=sa.String(length=255),
        existing_nullable=True,
    )
    op.add_column(
        "dataset_member",
        sa.Column("object_version_id", sa.String(length=255), nullable=True),
    )
