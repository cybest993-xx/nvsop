"""template：工位期望绑定和推理后端配置确认事实。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"

_REPORT_REJECTION_CODES = (
    "unknown_version",
    "version_station_mismatch",
    "digest_mismatch",
    "version_id_mismatch",
    "future_revision",
    "stale_revision",
    "conflicting_confirmation",
)


def upgrade() -> None:
    op.create_table(
        "template_station_binding",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("desired_version_id", sa.Uuid(), nullable=False),
        sa.Column("desired_sha256", sa.String(length=64), nullable=False),
        sa.Column("desired_config_revision", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "desired_config_revision > 0",
            name=op.f("ck_template_station_binding_desired_config_revision_positive"),
        ),
        sa.CheckConstraint(
            "revision > 0",
            name=op.f("ck_template_station_binding_revision_positive"),
        ),
        sa.CheckConstraint(
            "char_length(desired_sha256) = 64 AND desired_sha256 ~ '^[0-9A-Fa-f]{64}$'",
            name=op.f("ck_template_station_binding_desired_sha256"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_template_station_binding_station_id_device_station"),
        ),
        sa.ForeignKeyConstraint(
            ["desired_version_id"],
            ["template_version.id"],
            name=op.f("fk_template_station_binding_desired_version_id_template_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_station_binding")),
        sa.UniqueConstraint("station_id", name=op.f("uq_template_station_binding_station_id")),
    )
    op.create_index(
        op.f("ix_template_station_binding_desired_version_id"),
        "template_station_binding",
        ["desired_version_id"],
    )

    op.create_table(
        "template_configuration_report",
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("backend_id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("reported_version_id", sa.Uuid(), nullable=True),
        sa.Column("reported_sha256", sa.String(length=64), nullable=True),
        sa.Column("reported_config_revision", sa.Integer(), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_rejection_code",
            sa.Enum(*_REPORT_REJECTION_CODES, name="template_report_rejection_code"),
            nullable=True,
        ),
        sa.Column("last_rejection_detail", sa.String(length=255), nullable=True),
        sa.Column("last_rejection_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("updated_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(reported_version_id IS NULL AND reported_sha256 IS NULL "
            "AND reported_config_revision IS NULL AND reported_at IS NULL) OR "
            "(reported_version_id IS NOT NULL AND reported_sha256 IS NOT NULL "
            "AND reported_config_revision IS NOT NULL AND reported_at IS NOT NULL)",
            name=op.f("ck_template_configuration_report_reported_fields_complete"),
        ),
        sa.CheckConstraint(
            "reported_sha256 IS NULL OR "
            "(char_length(reported_sha256) = 64 AND reported_sha256 ~ '^[0-9A-Fa-f]{64}$')",
            name=op.f("ck_template_configuration_report_reported_sha256"),
        ),
        sa.CheckConstraint(
            "reported_config_revision IS NULL OR reported_config_revision > 0",
            name=op.f("ck_template_configuration_report_reported_config_revision_positive"),
        ),
        sa.CheckConstraint(
            "(last_rejection_code IS NULL AND last_rejection_detail IS NULL "
            "AND last_rejection_at IS NULL) OR "
            "(last_rejection_code IS NOT NULL AND last_rejection_detail IS NOT NULL "
            "AND last_rejection_at IS NOT NULL)",
            name=op.f("ck_template_configuration_report_rejection_fields_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_template_configuration_report_station_id_device_station"),
        ),
        sa.ForeignKeyConstraint(
            ["backend_id"],
            ["device_inference_backend.id"],
            name=op.f("fk_template_configuration_report_backend_id_device_inference_backend"),
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_template_configuration_report_host_id_device_inference_host"),
        ),
        sa.ForeignKeyConstraint(
            ["reported_version_id"],
            ["template_version.id"],
            name=op.f("fk_template_configuration_report_reported_version_id_template_version"),
        ),
        sa.PrimaryKeyConstraint(
            "station_id",
            "backend_id",
            name=op.f("pk_template_configuration_report"),
        ),
        sa.UniqueConstraint(
            "station_id",
            "backend_id",
            name=op.f("uq_template_configuration_report_station_id_backend_id"),
        ),
    )
    op.create_index(
        op.f("ix_template_configuration_report_host_id"),
        "template_configuration_report",
        ["host_id"],
    )
    op.create_index(
        op.f("ix_template_configuration_report_reported_version_id"),
        "template_configuration_report",
        ["reported_version_id"],
    )
    op.create_index(
        op.f("ix_template_configuration_report_last_rejection_at"),
        "template_configuration_report",
        ["last_rejection_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_template_configuration_report_last_rejection_at"),
        table_name="template_configuration_report",
    )
    op.drop_index(
        op.f("ix_template_configuration_report_reported_version_id"),
        table_name="template_configuration_report",
    )
    op.drop_index(
        op.f("ix_template_configuration_report_host_id"),
        table_name="template_configuration_report",
    )
    op.drop_table("template_configuration_report")
    sa.Enum(name="template_report_rejection_code").drop(op.get_bind())
    op.drop_index(
        op.f("ix_template_station_binding_desired_version_id"),
        table_name="template_station_binding",
    )
    op.drop_table("template_station_binding")
