"""device：持久化委托命令及推理机领取租约。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"


def upgrade() -> None:
    op.create_table(
        "device_pending_command",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column(
            "command_type",
            sa.Enum(
                "test_connector_connection",
                name="device_pending_command_type",
            ),
            nullable=False,
        ),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("target_revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "claimed",
                "succeeded",
                "failed",
                "rejected",
                name="device_pending_command_status",
            ),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(length=128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "result",
            postgresql.ENUM(
                "unverified",
                "reachable",
                "unreachable",
                name="device_connector_reachability",
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column("result_detail", sa.String(length=255), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "char_length(idempotency_key) > 0",
            name=op.f("ck_device_pending_command_idempotency_key_nonempty"),
        ),
        sa.CheckConstraint(
            "target_revision > 0",
            name=op.f("ck_device_pending_command_target_revision_positive"),
        ),
        sa.CheckConstraint(
            "attempt >= 0",
            name=op.f("ck_device_pending_command_attempt_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_device_pending_command_host_id_device_inference_host"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_pending_command")),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_device_pending_command_idempotency_key"),
        ),
    )
    op.create_index(
        op.f("ix_device_pending_command_host_id"),
        "device_pending_command",
        ["host_id"],
    )
    op.create_index(
        op.f("ix_device_pending_command_target_id"),
        "device_pending_command",
        ["target_id"],
    )
    op.create_index(
        op.f("ix_device_pending_command_status"),
        "device_pending_command",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_device_pending_command_status"), table_name="device_pending_command")
    op.drop_index(op.f("ix_device_pending_command_target_id"), table_name="device_pending_command")
    op.drop_index(op.f("ix_device_pending_command_host_id"), table_name="device_pending_command")
    op.drop_table("device_pending_command")
    sa.Enum(name="device_pending_command_status").drop(op.get_bind())
    sa.Enum(name="device_pending_command_type").drop(op.get_bind())
