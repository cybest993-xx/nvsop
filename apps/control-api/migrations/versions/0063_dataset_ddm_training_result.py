"""dataset：DDM 训练命令的观测时间、查询诊断与结果登记事实。

S074 把「应用接受/派发」「基座原状态」与「结果可读登记」分成三个持久事实：``last_observed_at``
记录最后一次成功读取基座原状态的时刻，``observation_failure_code/detail`` 单独记录查询失败，
``result_*`` 记录真实配置与 checkpoint 登记。``completed`` 是基座成功且结果登记后的唯一发布
终态，``failed`` 是确定的终止失败；两者都不再属于在途保护。

Revision ID: 0063
Revises: 0062
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"


def upgrade() -> None:
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("observation_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("observation_failure_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("observation_failure_detail", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column(
            "result_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_registered",
        ),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_generation", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_config_object_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_config_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_config_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_checkpoint_object_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_checkpoint_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_checkpoint_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_failure_code", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_failure_detail", sa.String(length=1024), nullable=True),
    )
    op.add_column(
        "dataset_ddm_training_command",
        sa.Column("result_registered_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dataset_ddm_training_command", "observation_attempted_at")
    op.drop_column("dataset_ddm_training_command", "result_registered_at")
    op.drop_column("dataset_ddm_training_command", "result_failure_detail")
    op.drop_column("dataset_ddm_training_command", "result_failure_code")
    op.drop_column("dataset_ddm_training_command", "result_checkpoint_size")
    op.drop_column("dataset_ddm_training_command", "result_checkpoint_sha256")
    op.drop_column("dataset_ddm_training_command", "result_checkpoint_object_key")
    op.drop_column("dataset_ddm_training_command", "result_config_size")
    op.drop_column("dataset_ddm_training_command", "result_config_sha256")
    op.drop_column("dataset_ddm_training_command", "result_config_object_key")
    op.drop_column("dataset_ddm_training_command", "result_generation")
    op.drop_column("dataset_ddm_training_command", "result_status")
    op.drop_column("dataset_ddm_training_command", "observation_failure_detail")
    op.drop_column("dataset_ddm_training_command", "observation_failure_code")
    op.drop_column("dataset_ddm_training_command", "last_observed_at")
