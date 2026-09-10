"""dataset：训练数据集、视频成员和独立上传尝试。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"


def upgrade() -> None:
    op.create_table(
        "dataset_training_dataset",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_training_dataset")),
    )
    op.create_table(
        "dataset_member",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=255), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("declared_sha256", sa.String(length=64), nullable=False),
        sa.Column("current_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("actual_size", sa.BigInteger(), nullable=True),
        sa.Column("actual_sha256", sa.String(length=64), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("codec", sa.String(length=64), nullable=True),
        sa.Column("container", sa.String(length=128), nullable=True),
        sa.Column("object_key", sa.String(length=512), nullable=True),
        sa.Column("object_version_id", sa.String(length=255), nullable=True),
        sa.Column("validation_job_id", sa.Uuid(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.String(length=1024), nullable=True),
        sa.Column("recovery_action", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "declared_size > 0",
            name=op.f("ck_dataset_member_declared_size_positive"),
        ),
        sa.CheckConstraint(
            "char_length(declared_sha256) = 64",
            name=op.f("ck_dataset_member_declared_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_member_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_member")),
    )
    op.create_table(
        "dataset_upload_attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("declared_sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("validation_job_id", sa.Uuid(), nullable=True),
        sa.Column("object_version_id", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "declared_size > 0",
            name=op.f("ck_dataset_upload_attempt_declared_size_positive"),
        ),
        sa.CheckConstraint(
            "char_length(declared_sha256) = 64",
            name=op.f("ck_dataset_upload_attempt_declared_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_upload_attempt_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["dataset_member.id"],
            name=op.f("fk_dataset_upload_attempt_member_id_dataset_member"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_upload_attempt")),
        sa.UniqueConstraint(
            "dataset_id",
            "idempotency_key",
            name=op.f("uq_dataset_upload_attempt_dataset_id"),
        ),
        sa.UniqueConstraint("object_key", name=op.f("uq_dataset_upload_attempt_object_key")),
    )
    op.create_index(op.f("ix_dataset_member_dataset_id"), "dataset_member", ["dataset_id"])
    op.create_index(
        op.f("ix_dataset_member_current_attempt_id"), "dataset_member", ["current_attempt_id"]
    )
    op.create_index(
        op.f("ix_dataset_upload_attempt_dataset_id"),
        "dataset_upload_attempt",
        ["dataset_id"],
    )
    op.create_index(
        op.f("ix_dataset_upload_attempt_member_id"),
        "dataset_upload_attempt",
        ["member_id"],
    )
    op.create_index(
        op.f("ix_dataset_upload_attempt_idempotency_key"),
        "dataset_upload_attempt",
        ["idempotency_key"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_dataset_upload_attempt_idempotency_key"), table_name="dataset_upload_attempt"
    )
    op.drop_index(op.f("ix_dataset_upload_attempt_member_id"), table_name="dataset_upload_attempt")
    op.drop_index(op.f("ix_dataset_upload_attempt_dataset_id"), table_name="dataset_upload_attempt")
    op.drop_index(op.f("ix_dataset_member_current_attempt_id"), table_name="dataset_member")
    op.drop_index(op.f("ix_dataset_member_dataset_id"), table_name="dataset_member")
    op.drop_table("dataset_upload_attempt")
    op.drop_table("dataset_member")
    op.drop_table("dataset_training_dataset")
