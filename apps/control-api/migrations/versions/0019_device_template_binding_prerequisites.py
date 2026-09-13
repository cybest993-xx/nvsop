"""device：为模板绑定补齐工位运行参数和后端版本外键。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | None = "0018"


def upgrade() -> None:
    runtime_mode = postgresql.ENUM(
        "follow_template",
        "custom",
        name="device_runtime_parameter_mode",
        create_type=False,
    )
    runtime_mode.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "device_station",
        sa.Column(
            "runtime_parameter_mode",
            sa.Enum(
                "follow_template",
                "custom",
                name="device_runtime_parameter_mode",
            ),
            nullable=False,
            server_default="follow_template",
        ),
    )
    op.add_column(
        "device_station",
        sa.Column("runtime_parameter_overrides", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "device_station",
        sa.Column(
            "runtime_parameters_revision",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.alter_column(
        "device_station",
        "runtime_parameter_mode",
        existing_type=sa.Enum(
            "follow_template",
            "custom",
            name="device_runtime_parameter_mode",
            create_type=False,
        ),
        server_default=None,
    )
    op.alter_column(
        "device_station",
        "runtime_parameters_revision",
        existing_type=sa.Integer(),
        server_default=None,
    )
    op.create_foreign_key(
        op.f("fk_device_inference_backend_template_version_id_template_version"),
        "device_inference_backend",
        "template_version",
        ["template_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_device_inference_backend_template_version_id_template_version"),
        "device_inference_backend",
        type_="foreignkey",
    )
    op.drop_column("device_station", "runtime_parameters_revision")
    op.drop_column("device_station", "runtime_parameter_overrides")
    op.drop_column("device_station", "runtime_parameter_mode")
    sa.Enum(name="device_runtime_parameter_mode").drop(op.get_bind())
