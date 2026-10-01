"""retention：全局保留策略单行表（§5.19）。录像滚动窗口/原始素材窗口不在此表。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045"
down_revision: str | None = "0044"


def upgrade() -> None:
    op.create_table(
        "retention_policy",
        sa.Column("singleton", sa.SmallInteger(), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("singleton", name=op.f("pk_retention_policy")),
    )


def downgrade() -> None:
    op.drop_table("retention_policy")
