"""device：为每台推理机持久化配置内容版本。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"

RAW_SQL_TABLES = frozenset(
    {
        "device_camera",
        "device_connector",
        "device_inference_backend",
        "device_inference_host",
        "device_point",
        "device_station",
    }
)


def upgrade() -> None:
    op.add_column(
        "device_inference_host",
        sa.Column(
            "configuration_revision",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "device_inference_host",
        sa.Column("configuration_sha256", sa.String(length=64), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_device_inference_host_configuration_revision_nonnegative"),
        "device_inference_host",
        "configuration_revision >= 0",
    )
    op.create_check_constraint(
        op.f("ck_device_inference_host_configuration_sha256"),
        "device_inference_host",
        "configuration_sha256 IS NULL OR length(configuration_sha256) = 64",
    )
    # 旧 bundle 使用存活设备对象 revision 的最大值。先把持久化计数器至少回填到该值，
    # 避免迁移后首次内容变化被已确认过该 bundle 的 edge 拒绝；后续由 repository 持续递增，
    # 因此删除对象后仍保持单调。
    op.execute(
        sa.text(
            """
            UPDATE device_inference_host AS host
               SET configuration_revision = GREATEST(
                   host.revision,
                   COALESCE((
                       SELECT max(backend.revision)
                         FROM device_inference_backend AS backend
                        WHERE backend.host_id = host.id
                   ), 0),
                   COALESCE((
                       SELECT max(camera.revision)
                         FROM device_camera AS camera
                        WHERE camera.host_id = host.id
                   ), 0),
                   COALESCE((
                       SELECT max(connector.revision)
                         FROM device_connector AS connector
                        WHERE connector.host_id = host.id
                   ), 0),
                   COALESCE((
                       SELECT max(point.revision)
                         FROM device_point AS point
                         JOIN device_connector AS connector
                           ON connector.id = point.connector_id
                        WHERE connector.host_id = host.id
                   ), 0),
                   COALESCE((
                       SELECT max(station.revision)
                         FROM device_station AS station
                         JOIN device_camera AS camera
                           ON camera.station_id = station.id
                        WHERE camera.host_id = host.id
                   ), 0),
                   COALESCE((
                       SELECT max(station.runtime_parameters_revision)
                         FROM device_station AS station
                         JOIN device_camera AS camera
                           ON camera.station_id = station.id
                        WHERE camera.host_id = host.id
                   ), 0),
                   0
               )
            """
        )
    )
    op.alter_column("device_inference_host", "configuration_revision", server_default=None)


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_device_inference_host_configuration_sha256"),
        "device_inference_host",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_device_inference_host_configuration_revision_nonnegative"),
        "device_inference_host",
        type_="check",
    )
    op.drop_column("device_inference_host", "configuration_sha256")
    op.drop_column("device_inference_host", "configuration_revision")
