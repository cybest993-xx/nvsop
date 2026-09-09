"""job：持久化异步应用任务和事务性 outbox 状态。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"


def upgrade() -> None:
    op.create_table(
        "job_application_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("member_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outbox_status", sa.String(length=32), nullable=False),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False),
        sa.Column("last_dispatch_error", sa.String(length=1024), nullable=True),
        sa.CheckConstraint(
            "dispatch_attempts >= 0",
            name=op.f("ck_job_application_job_dispatch_attempts_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_application_job")),
        sa.UniqueConstraint(
            "job_type",
            "attempt_id",
            name=op.f("uq_job_application_job_job_type"),
        ),
    )
    op.create_index(
        op.f("ix_job_application_job_member_id"),
        "job_application_job",
        ["member_id"],
    )
    op.create_index(
        op.f("ix_job_application_job_attempt_id"),
        "job_application_job",
        ["attempt_id"],
    )
    op.create_index(
        op.f("ix_job_application_job_outbox_status"),
        "job_application_job",
        ["outbox_status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_job_application_job_outbox_status"), table_name="job_application_job")
    op.drop_index(op.f("ix_job_application_job_attempt_id"), table_name="job_application_job")
    op.drop_index(op.f("ix_job_application_job_member_id"), table_name="job_application_job")
    op.drop_table("job_application_job")
