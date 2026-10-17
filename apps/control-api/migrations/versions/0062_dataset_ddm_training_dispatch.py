"""dataset：DDM 训练命令的派发意图、在途保护与基座任务绑定。

S073 在命令行上持久化发送前意图（逻辑基座目标、发送前任务集合、实际消费事实）与真实基座
任务身份；``backend_target`` 的部分唯一索引保证每个逻辑基座最多一条在途命令，独立于通用
任务租约。``bound``/``result_unknown`` 保持在途保护，释放属于 S074。

Revision ID: 0062
Revises: 0061
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0062"
down_revision: str | None = "0061"


def upgrade() -> None:
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("backend_target", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("base_job_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("baseline_job_ids", JSONB(), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("dispatch_facts", JSONB(), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("base_observation", JSONB(), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("failure_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("failure_detail", sa.String(length=1024), nullable=True),
    )
    op.create_index(
        "uq_dataset_ddm_training_command_backend_target_inflight",
        "dataset_ddm_training_command",
        ["backend_target"],
        unique=True,
        postgresql_where=sa.text("status IN ('result_unknown', 'bound')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_dataset_ddm_training_command_backend_target_inflight",
        table_name="dataset_ddm_training_command",
    )
    op.drop_column("dataset_ddm_training_command", "failure_detail")
    op.drop_column("dataset_ddm_training_command", "failure_code")
    op.drop_column("dataset_ddm_training_command", "base_observation")
    op.drop_column("dataset_ddm_training_command", "dispatch_facts")
    op.drop_column("dataset_ddm_training_command", "baseline_job_ids")
    op.drop_column("dataset_ddm_training_command", "base_job_id")
    op.drop_column("dataset_ddm_training_command", "backend_target")
