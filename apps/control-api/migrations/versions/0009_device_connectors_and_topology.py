"""device：连接器配置及其与现有设备拓扑的互斥约束。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"

RAW_SQL_TABLES = frozenset(
    {
        "device_camera",
        "device_connector",
        "device_inference_host",
        "device_station",
    }
)


def upgrade() -> None:
    op.create_table(
        "device_connector",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "connector_type",
            sa.Enum("hikvision_isapi", "board_card", name="device_connector_type"),
            nullable=False,
        ),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("credentials_configured", sa.Boolean(), nullable=False),
        sa.Column(
            "reachability",
            sa.Enum("unverified", "reachable", "unreachable", name="device_connector_reachability"),
            nullable=False,
        ),
        sa.Column("health_detail", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM("active", "deactivated", name="device_status", create_type=False),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("char_length(name) > 0", name=op.f("ck_device_connector_name_nonempty")),
        sa.CheckConstraint(
            "jsonb_typeof(configuration) = 'object'"
            " AND jsonb_typeof(configuration->'address') = 'string'"
            " AND char_length(configuration->>'address') > 0"
            " AND (NOT (configuration ? 'port')"
            " OR (jsonb_typeof(configuration->'port') = 'number'"
            " AND (configuration->>'port')::integer BETWEEN 1 AND 65535))"
            " AND configuration->>'address' NOT LIKE '%?%'"
            " AND configuration->>'address' NOT LIKE '%#%'"
            " AND configuration->>'address' NOT LIKE '%@%'",
            name=op.f("ck_device_connector_configuration_safe"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_device_connector_station_id_device_station"),
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_device_connector_host_id_device_inference_host"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_connector")),
        sa.UniqueConstraint("station_id", "name", name=op.f("uq_device_connector_station_id_name")),
    )
    op.create_index(op.f("ix_device_connector_station_id"), "device_connector", ["station_id"])
    op.create_index(op.f("ix_device_connector_host_id"), "device_connector", ["host_id"])
    op.execute(sa.text(_CONNECTOR_TOPOLOGY_FUNCTION))
    op.execute(sa.text(_CONNECTOR_TOPOLOGY_TRIGGER))
    op.execute(sa.text(_CAMERA_CONNECTOR_FUNCTION))
    op.execute(sa.text(_CAMERA_CONNECTOR_TRIGGER))


_CONNECTOR_TOPOLOGY_FUNCTION = """
CREATE FUNCTION device_assert_connector_topology() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target_station_status text;
    target_host_status text;
BEGIN
    IF TG_OP = 'INSERT'
       OR NEW.station_id IS DISTINCT FROM OLD.station_id
       OR NEW.host_id IS DISTINCT FROM OLD.host_id THEN
        -- 连接器沿用相机的锁后缀顺序：先推理机，再工位；相机的既有 0007
        -- 触发器已经先锁后端、推理机、工位。这样父后端变更与相机变更不会反向取锁。
        PERFORM 1 FROM device_inference_host WHERE id = NEW.host_id FOR UPDATE;
        SELECT status::text INTO target_host_status
          FROM device_inference_host WHERE id = NEW.host_id;
        IF NOT FOUND THEN
            RETURN NEW;
        END IF;

        PERFORM 1 FROM device_station WHERE id = NEW.station_id FOR UPDATE;
        SELECT status::text INTO target_station_status
          FROM device_station WHERE id = NEW.station_id;
        IF NOT FOUND THEN
            RETURN NEW;
        END IF;
        IF target_station_status <> 'active' THEN
            RAISE EXCEPTION 'device_connector_station_deactivated' USING ERRCODE = 'P0001';
        END IF;
        IF target_host_status <> 'active' THEN
            RAISE EXCEPTION 'device_connector_host_deactivated' USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (
            SELECT 1 FROM device_camera
             WHERE station_id = NEW.station_id
               AND host_id IS DISTINCT FROM NEW.host_id
        ) THEN
            RAISE EXCEPTION 'device_connector_station_host_conflict' USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (
            SELECT 1 FROM device_connector
             WHERE station_id = NEW.station_id
               AND id <> NEW.id
               AND host_id IS DISTINCT FROM NEW.host_id
        ) THEN
            RAISE EXCEPTION 'device_connector_station_host_conflict' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

_CONNECTOR_TOPOLOGY_TRIGGER = """
CREATE TRIGGER device_connector_topology_must_be_consistent
    BEFORE INSERT OR UPDATE OF station_id, host_id ON device_connector
    FOR EACH ROW EXECUTE FUNCTION device_assert_connector_topology()
"""

_CAMERA_CONNECTOR_FUNCTION = """
CREATE FUNCTION device_assert_camera_connector_topology() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT'
       OR NEW.station_id IS DISTINCT FROM OLD.station_id
       OR NEW.host_id IS DISTINCT FROM OLD.host_id THEN
        PERFORM 1 FROM device_station WHERE id = NEW.station_id FOR UPDATE;
        IF EXISTS (
            SELECT 1 FROM device_connector
             WHERE station_id = NEW.station_id
               AND host_id IS DISTINCT FROM NEW.host_id
        ) THEN
            RAISE EXCEPTION 'device_connector_station_host_conflict' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

_CAMERA_CONNECTOR_TRIGGER = """
CREATE TRIGGER device_camera_connector_topology_must_be_consistent
    AFTER INSERT OR UPDATE OF station_id, host_id ON device_camera
    FOR EACH ROW EXECUTE FUNCTION device_assert_camera_connector_topology()
"""


def downgrade() -> None:
    op.execute(
        sa.text("DROP TRIGGER device_camera_connector_topology_must_be_consistent ON device_camera")
    )
    op.execute(
        sa.text("DROP TRIGGER device_connector_topology_must_be_consistent ON device_connector")
    )
    op.execute(sa.text("DROP FUNCTION device_assert_camera_connector_topology()"))
    op.execute(sa.text("DROP FUNCTION device_assert_connector_topology()"))
    op.drop_index(op.f("ix_device_connector_host_id"), table_name="device_connector")
    op.drop_index(op.f("ix_device_connector_station_id"), table_name="device_connector")
    op.drop_table("device_connector")
    sa.Enum(name="device_connector_reachability").drop(op.get_bind())
    sa.Enum(name="device_connector_type").drop(op.get_bind())
