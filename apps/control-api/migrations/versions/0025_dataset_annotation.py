"""dataset：动作清单修订、标注上下文和不可变切片候选。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025"
down_revision: str | None = "0024"


def upgrade() -> None:
    op.create_table(
        "dataset_action_list_revision",
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("actions", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_action_list_revision_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "dataset_id",
            "revision",
            name=op.f("pk_dataset_action_list_revision"),
        ),
    )
    op.create_table(
        "dataset_annotation_context",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("action_list_revision", sa.Integer(), nullable=False),
        sa.Column("annotation_revision", sa.Integer(), nullable=False),
        sa.Column("source_object_version_id", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("upstream_data_id", sa.String(length=255), nullable=True),
        sa.Column("upstream_video_id", sa.String(length=255), nullable=True),
        sa.Column("upstream_video_size", sa.BigInteger(), nullable=True),
        sa.Column("upstream_video_sha256", sa.String(length=64), nullable=True),
        sa.Column("upstream_video_duration_seconds", sa.Float(), nullable=True),
        sa.Column("preparation_job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "preparation_status", sa.String(length=32), nullable=False, server_default="pending"
        ),
        sa.Column("preparation_failure_code", sa.String(length=64), nullable=True),
        sa.Column("preparation_failure_detail", sa.String(length=1024), nullable=True),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_annotation_context_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["dataset_member.id"],
            name=op.f("fk_dataset_annotation_context_member_id_dataset_member"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_annotation_context")),
        sa.CheckConstraint(
            "annotation_revision >= 0",
            name=op.f("ck_dataset_annotation_context_annotation_revision"),
        ),
        sa.CheckConstraint(
            "char_length(source_sha256) = 64",
            name=op.f("ck_dataset_annotation_context_source_sha256"),
        ),
    )
    op.create_index(
        op.f("ix_dataset_annotation_context_member_id"),
        "dataset_annotation_context",
        ["member_id"],
    )
    op.create_index(
        op.f("ix_dataset_annotation_context_preparation_job_id"),
        "dataset_annotation_context",
        ["preparation_job_id"],
    )
    op.create_table(
        "dataset_annotation_submission",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("context_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action_list_revision", sa.Integer(), nullable=False),
        sa.Column("source_object_version_id", sa.String(length=255), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("segments", postgresql.JSONB(), nullable=False),
        sa.Column("raw_segments", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_annotation_submission_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["member_id"],
            ["dataset_member.id"],
            name=op.f("fk_dataset_annotation_submission_member_id_dataset_member"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["context_id"],
            ["dataset_annotation_context.id"],
            name=op.f("fk_dataset_annotation_submission_context_id_dataset_annotation_context"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_annotation_submission")),
        sa.UniqueConstraint(
            "dataset_id",
            "idempotency_key",
            name=op.f("uq_dataset_annotation_submission_dataset_id"),
        ),
        sa.UniqueConstraint(
            "member_id",
            "revision",
            name=op.f("uq_dataset_annotation_submission_member_id_revision"),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_dataset_annotation_submission_revision"),
        ),
        sa.CheckConstraint(
            "char_length(source_sha256) = 64",
            name=op.f("ck_dataset_annotation_submission_source_sha256"),
        ),
    )
    op.create_index(
        op.f("ix_dataset_annotation_submission_member_id"),
        "dataset_annotation_submission",
        ["member_id"],
    )
    op.create_table(
        "dataset_annotation_execution",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("clips", postgresql.JSONB(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("upstream_data_id", sa.String(length=255), nullable=True),
        sa.Column("upstream_video_id", sa.String(length=255), nullable=True),
        sa.Column("derived_video_size", sa.BigInteger(), nullable=True),
        sa.Column("derived_video_sha256", sa.String(length=64), nullable=True),
        sa.Column("derived_video_duration_seconds", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["dataset_annotation_submission.id"],
            name=op.f(
                "fk_dataset_annotation_execution_submission_id_dataset_annotation_submission"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_annotation_execution")),
        sa.UniqueConstraint(
            "submission_id",
            "generation",
            name=op.f("uq_dataset_annotation_execution_submission_id"),
        ),
        sa.UniqueConstraint("job_id", name=op.f("uq_dataset_annotation_execution_job_id")),
        sa.CheckConstraint(
            "generation > 0", name=op.f("ck_dataset_annotation_execution_generation")
        ),
    )
    op.create_index(
        op.f("ix_dataset_annotation_execution_submission_id"),
        "dataset_annotation_execution",
        ["submission_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_dataset_annotation_execution_submission_id"),
        table_name="dataset_annotation_execution",
    )
    op.drop_table("dataset_annotation_execution")
    op.drop_index(
        op.f("ix_dataset_annotation_submission_member_id"),
        table_name="dataset_annotation_submission",
    )
    op.drop_table("dataset_annotation_submission")
    op.drop_index(
        op.f("ix_dataset_annotation_context_preparation_job_id"),
        table_name="dataset_annotation_context",
    )
    op.drop_index(
        op.f("ix_dataset_annotation_context_member_id"),
        table_name="dataset_annotation_context",
    )
    op.drop_table("dataset_annotation_context")
    op.drop_table("dataset_action_list_revision")
