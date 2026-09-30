"""monitor：流健康镜像按流身份归档，保存每路观测有效性与时间锚偏移。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"


def upgrade() -> None:
    op.add_column(
        "monitor_reported_health",
        sa.Column("stream_id", sa.String(length=255), nullable=True),
    )
    op.create_index(
        op.f("ix_monitor_reported_health_stream_id"),
        "monitor_reported_health",
        ["stream_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_monitor_reported_health_stream_id"), table_name="monitor_reported_health"
    )
    op.drop_column("monitor_reported_health", "stream_id")
