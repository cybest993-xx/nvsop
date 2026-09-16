"""device：持久化不可变的主机配置归属历史。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034"
down_revision: str | None = "0033"

RAW_SQL_TABLES = frozenset({"device_configuration_assignment"})


def upgrade() -> None:
    op.create_table(
        "device_configuration_assignment",
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("backend_id", sa.Uuid(), nullable=False),
        sa.Column("configuration_sha256", sa.String(length=64), nullable=False),
        sa.Column("template_version_id", sa.Uuid(), nullable=True),
        sa.Column("template_sha256", sa.String(length=64), nullable=True),
        sa.Column("model_ids", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "configuration_revision > 0",
            name=op.f("ck_device_configuration_assignment_revision_positive"),
        ),
        sa.CheckConstraint(
            "length(configuration_sha256) = 64",
            name=op.f("ck_device_configuration_assignment_configuration_sha256"),
        ),
        sa.CheckConstraint(
            "template_sha256 IS NULL OR length(template_sha256) = 64",
            name=op.f("ck_device_configuration_assignment_template_sha256"),
        ),
        sa.CheckConstraint(
            "(template_version_id IS NULL) = (template_sha256 IS NULL)",
            name=op.f("ck_device_configuration_assignment_template_pair"),
        ),
        sa.PrimaryKeyConstraint(
            "host_id",
            "configuration_revision",
            "station_id",
            "backend_id",
            name=op.f("pk_device_configuration_assignment"),
        ),
    )


def downgrade() -> None:
    op.drop_table("device_configuration_assignment")
