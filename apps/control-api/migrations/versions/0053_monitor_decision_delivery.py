"""Archive event latch time and advisory live delivery outside the decision fact."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"


def upgrade() -> None:
    op.add_column("monitor_reported_decision", sa.Column("latched_at", sa.Text(), nullable=True))
    op.add_column(
        "monitor_reported_decision",
        sa.Column("realtime", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("monitor_reported_decision", "realtime")
    op.drop_column("monitor_reported_decision", "latched_at")
