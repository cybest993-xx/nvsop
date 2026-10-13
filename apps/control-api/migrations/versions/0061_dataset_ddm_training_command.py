"""dataset：DDM 训练应用的持久命令与待派发事实。

命令把授权上下文、独立训练/验证冻结输入与按角色消费参数绑定到一个应用命令身份；
S073/S074 安全派发就绪前，命令与它的 outbox 任务只保持 ``pending``，不注册可实际
start 的公开操作。

Revision ID: 0061
Revises: 0060
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0061"
down_revision: str | None = "0060"


def upgrade() -> None:
    op.create_table(
        "dataset_ddm_training_command",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["dataset_training_dataset.id"],
            name=op.f("fk_dataset_ddm_training_command_dataset_id_dataset_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dataset_ddm_training_command")),
        sa.UniqueConstraint(
            "dataset_id",
            "idempotency_key",
            name=op.f("uq_dataset_ddm_training_command_dataset_id_idempotency_key"),
        ),
        sa.UniqueConstraint(
            "job_id",
            name=op.f("uq_dataset_ddm_training_command_job_id"),
        ),
    )
    op.create_index(
        op.f("ix_dataset_ddm_training_command_dataset_id"),
        "dataset_ddm_training_command",
        ["dataset_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_dataset_ddm_training_command_dataset_id"),
        table_name="dataset_ddm_training_command",
    )
    op.drop_table("dataset_ddm_training_command")
