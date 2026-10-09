"""Mirror the latest Edge execution-authority state per station and host."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054"
down_revision: str | None = "0053"


def upgrade() -> None:
    op.create_table(
        "monitor_execution_authority",
        sa.Column("station_id", sa.String(128), primary_key=True),
        sa.Column("host_id", sa.String(128), primary_key=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
    )
    op.create_index(
        op.f("ix_monitor_execution_authority_reported_at"),
        "monitor_execution_authority",
        ["reported_at"],
    )
    op.create_index(
        op.f("ix_monitor_execution_authority_received_at"),
        "monitor_execution_authority",
        ["received_at"],
    )


def downgrade() -> None:
    op.drop_table("monitor_execution_authority")
