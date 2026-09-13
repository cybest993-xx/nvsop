"""job：为数据集用途任务记录所属数据集。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"


def upgrade() -> None:
    op.add_column(
        "job_application_job",
        sa.Column("dataset_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        op.f("ix_job_application_job_dataset_id"),
        "job_application_job",
        ["dataset_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_job_application_job_dataset_id"), table_name="job_application_job")
    op.drop_column("job_application_job", "dataset_id")
