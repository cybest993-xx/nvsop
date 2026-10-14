"""retention：工位整组保留覆盖记录（§5.19）。

retention 拥有覆盖记录本身：每个工位最多一行，保存「整组跟随全局默认 / 整组自定义」的模式、覆盖组
与记录版本。没有本行即该工位整组跟随全局默认。录像滚动窗口仍按推理机配置，不在此表。

Revision ID: 0064
Revises: 0063
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0064"
down_revision: str | None = "0063"


def upgrade() -> None:
    retention_mode = postgresql.ENUM(
        "follow_global",
        "custom",
        name="retention_station_policy_mode",
        create_type=False,
    )
    retention_mode.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "retention_station_policy",
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column(
            "mode",
            postgresql.ENUM(
                "follow_global",
                "custom",
                name="retention_station_policy_mode",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("overrides", postgresql.JSONB(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("station_id", name=op.f("pk_retention_station_policy")),
    )


def downgrade() -> None:
    op.drop_table("retention_station_policy")
    postgresql.ENUM(
        "follow_global",
        "custom",
        name="retention_station_policy_mode",
        create_type=False,
    ).drop(op.get_bind(), checkfirst=True)
