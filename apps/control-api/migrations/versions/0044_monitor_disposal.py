"""monitor：镜像推理机本地处置结果，不取得处置执行权。"""

from __future__ import annotations
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: str | None = "0043"


def upgrade() -> None:
    op.create_table(
        "monitor_disposal",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("host_id", sa.String(128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(op.f("ix_monitor_disposal_host_id"), "monitor_disposal", ["host_id"])
    op.create_index(op.f("ix_monitor_disposal_received_at"), "monitor_disposal", ["received_at"])


def downgrade() -> None:
    op.drop_table("monitor_disposal")
