"""monitor：v2 historical decision 以 payload provenance 代替单一 backend 列。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"


def upgrade() -> None:
    op.alter_column(
        "monitor_reported_decision",
        "backend_id",
        existing_type=sa.String(length=128),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "monitor_reported_decision",
        "backend_id",
        existing_type=sa.String(length=128),
        nullable=False,
    )
