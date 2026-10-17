"""dataset：增强真实产物与不可提前释放的派发意图。

Revision ID: 0076
Revises: 0075
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_augmentation_command",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("annotation_execution_id", sa.Uuid(), nullable=False),
        sa.Column("label_data_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("frame_drop_iterations", sa.Integer(), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("job_id", sa.Uuid()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("backend_target", sa.String(2048)),
        sa.Column("base_augmentation_id", sa.String(255)),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("base_observation", postgresql.JSONB(), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(255)),
        sa.Column("result_status", sa.String(32), nullable=False),
        sa.Column("result_manifest", postgresql.JSONB(), nullable=False),
        sa.Column("result_failure_code", sa.String(255)),
        sa.Column("cancelled_by", sa.Uuid()),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_augmentation_command")),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_augmentation_command_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["annotation_execution_id"],
            ["dataset_annotation_execution.id"],
            name=op.f(
                "fk_dataset_augmentation_command_annotation_execution_id_dataset_annotation_execution"
            ),
        ),
        sa.UniqueConstraint("dataset_id", "idempotency_key", name="uq_augmentation_request"),
        sa.UniqueConstraint("backend_target", "base_augmentation_id", name="uq_augmentation_base"),
        sa.CheckConstraint(
            "frame_drop_iterations BETWEEN 0 AND 10", name="augmentation_iterations"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'result_unknown', 'bound', 'completed', 'failed', 'cancelled')",
            name="augmentation_status",
        ),
        sa.CheckConstraint(
            "status NOT IN ('result_unknown', 'bound', 'completed') OR "
            "(backend_target IS NOT NULL AND dispatched_at IS NOT NULL)",
            name="augmentation_intent",
        ),
        sa.CheckConstraint(
            "status NOT IN ('bound', 'completed') OR base_augmentation_id IS NOT NULL",
            name="augmentation_identity",
        ),
        sa.CheckConstraint(
            "result_status IN ('not_registered', 'registered') AND "
            "(status = 'completed') = (result_status = 'registered')",
            name="augmentation_registration",
        ),
        sa.CheckConstraint(
            "(status = 'cancelled') = (cancelled_at IS NOT NULL) AND "
            "(cancelled_at IS NULL) = (cancelled_by IS NULL)",
            name="augmentation_cancellation",
        ),
    )
    op.create_index(
        op.f("ix_dataset_augmentation_command_dataset_id"),
        "dataset_augmentation_command",
        ["dataset_id"],
    )
    op.create_index(
        "uq_augmentation_inflight",
        "dataset_augmentation_command",
        ["backend_target", "label_data_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('result_unknown', 'bound')"),
    )


def downgrade() -> None:
    op.drop_index("uq_augmentation_inflight", table_name="dataset_augmentation_command")
    op.drop_index(
        op.f("ix_dataset_augmentation_command_dataset_id"),
        table_name="dataset_augmentation_command",
    )
    op.drop_table("dataset_augmentation_command")
