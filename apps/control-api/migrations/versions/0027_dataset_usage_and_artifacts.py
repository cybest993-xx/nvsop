"""dataset：用途检查、VLM 候选和不可覆盖派生制品。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0027"
down_revision: str | None = "0026"


def upgrade() -> None:
    op.create_table(
        "dataset_vlm_candidate",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("action_list_revision", sa.Integer(), nullable=False),
        sa.Column("records", JSONB(), nullable=False),
        sa.Column("media", JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_vlm_candidate_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_vlm_candidate")),
        sa.UniqueConstraint(
            "dataset_id",
            "revision",
            name=op.f("uq_dataset_vlm_candidate_dataset_revision"),
        ),
    )
    op.create_index(
        op.f("ix_dataset_vlm_candidate_dataset_id"),
        "dataset_vlm_candidate",
        ["dataset_id"],
    )

    op.create_table(
        "dataset_usage_check",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("input_snapshot", JSONB(), nullable=False),
        sa.Column("summary", JSONB(), nullable=False),
        sa.Column("issues", JSONB(), nullable=False),
        sa.Column("base_commit", sa.String(length=128), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_usage_check_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["dataset_vlm_candidate.id"],
            name=op.f("fk_dataset_usage_check_candidate_id_dataset_vlm_candidate"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_usage_check")),
        sa.UniqueConstraint("job_id", name=op.f("uq_dataset_usage_check_job_id")),
    )
    op.create_index(
        op.f("ix_dataset_usage_check_dataset_id"),
        "dataset_usage_check",
        ["dataset_id"],
    )
    op.create_index(
        op.f("ix_dataset_usage_check_kind"),
        "dataset_usage_check",
        ["kind"],
    )
    op.create_index(
        op.f("ix_dataset_usage_check_input_digest"),
        "dataset_usage_check",
        ["input_digest"],
    )

    op.create_table(
        "dataset_artifact",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("usage_check_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_digest", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=True),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=True),
        sa.Column("artifact_size", sa.BigInteger(), nullable=True),
        sa.Column("manifest", JSONB(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.String(length=1024), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recovery_action", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_artifact_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["usage_check_id"],
            ["dataset_usage_check.id"],
            name=op.f("fk_dataset_artifact_usage_check_id_dataset_usage_check"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_artifact")),
        sa.UniqueConstraint(
            "dataset_id",
            "input_digest",
            name=op.f("uq_dataset_artifact_dataset_input_digest"),
        ),
        sa.UniqueConstraint("object_key", name=op.f("uq_dataset_artifact_object_key")),
        sa.UniqueConstraint("job_id", name=op.f("uq_dataset_artifact_job_id")),
    )
    op.create_index(
        op.f("ix_dataset_artifact_dataset_id"),
        "dataset_artifact",
        ["dataset_id"],
    )
    op.create_index(
        op.f("ix_dataset_artifact_usage_check_id"),
        "dataset_artifact",
        ["usage_check_id"],
    )
    op.create_index(
        op.f("ix_dataset_artifact_input_digest"),
        "dataset_artifact",
        ["input_digest"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_dataset_artifact_input_digest"), table_name="dataset_artifact")
    op.drop_index(op.f("ix_dataset_artifact_usage_check_id"), table_name="dataset_artifact")
    op.drop_index(op.f("ix_dataset_artifact_dataset_id"), table_name="dataset_artifact")
    op.drop_table("dataset_artifact")
    op.drop_index(op.f("ix_dataset_usage_check_input_digest"), table_name="dataset_usage_check")
    op.drop_index(op.f("ix_dataset_usage_check_kind"), table_name="dataset_usage_check")
    op.drop_index(op.f("ix_dataset_usage_check_dataset_id"), table_name="dataset_usage_check")
    op.drop_table("dataset_usage_check")
    op.drop_index(op.f("ix_dataset_vlm_candidate_dataset_id"), table_name="dataset_vlm_candidate")
    op.drop_table("dataset_vlm_candidate")
