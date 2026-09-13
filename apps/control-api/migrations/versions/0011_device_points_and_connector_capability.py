"""device：授权点位、连接器能力声明及并发拓扑保护。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"

RAW_SQL_TABLES = frozenset(
    {
        "device_camera",
        "device_connector",
        "device_inference_host",
        "device_point",
        "device_station",
    }
)

_CAPABILITY_DEFAULT = sa.text('\'{"verification":"unverified"}\'::jsonb')
_CAPABILITY_KEYS = "ARRAY['verification','delivery','polling_interval_seconds','max_delivery_delay_seconds','sequencing','edge_preservation','timestamp_source']"


def upgrade() -> None:
    op.add_column(
        "device_connector",
        sa.Column(
            "capability",
            postgresql.JSONB(),
            nullable=False,
            server_default=_CAPABILITY_DEFAULT,
        ),
    )
    op.create_check_constraint(
        op.f("ck_device_connector_capability_valid"),
        "device_connector",
        _CAPABILITY_CHECK,
    )
    op.create_table(
        "device_point",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("connector_id", sa.Uuid(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("input", "output", name="device_point_direction"),
            nullable=False,
        ),
        sa.Column("identifier", sa.String(length=128), nullable=False),
        sa.Column("semantic_label", sa.String(length=128), nullable=False),
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
        sa.CheckConstraint(
            "char_length(identifier) > 0",
            name=op.f("ck_device_point_identifier_nonempty"),
        ),
        sa.CheckConstraint(
            "char_length(semantic_label) > 0",
            name=op.f("ck_device_point_semantic_label_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_device_point_station_id_device_station"),
        ),
        sa.ForeignKeyConstraint(
            ["connector_id"],
            ["device_connector.id"],
            name=op.f("fk_device_point_connector_id_device_connector"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_point")),
        sa.UniqueConstraint(
            "station_id",
            "semantic_label",
            name=op.f("uq_device_point_station_id_semantic_label"),
        ),
        sa.UniqueConstraint(
            "connector_id",
            "direction",
            "identifier",
            name=op.f("uq_device_point_connector_id_direction_identifier"),
        ),
    )
    op.create_index(op.f("ix_device_point_station_id"), "device_point", ["station_id"])
    op.create_index(op.f("ix_device_point_connector_id"), "device_point", ["connector_id"])
    op.execute(sa.text(_POINT_TOPOLOGY_FUNCTION))
    op.execute(sa.text(_POINT_TOPOLOGY_TRIGGER))
    op.execute(sa.text(_CONNECTOR_TOPOLOGY_WITH_POINTS))


def downgrade() -> None:
    op.execute(sa.text(_CONNECTOR_TOPOLOGY_WITHOUT_POINTS))
    op.execute(sa.text("DROP TRIGGER device_point_topology_must_be_consistent ON device_point"))
    op.execute(sa.text("DROP FUNCTION device_assert_point_topology()"))
    op.drop_index(op.f("ix_device_point_connector_id"), table_name="device_point")
    op.drop_index(op.f("ix_device_point_station_id"), table_name="device_point")
    op.drop_table("device_point")
    sa.Enum(name="device_point_direction").drop(op.get_bind())
    op.drop_constraint(
        op.f("ck_device_connector_capability_valid"),
        "device_connector",
        type_="check",
    )
    op.drop_column("device_connector", "capability")


_CAPABILITY_CHECK = f"""
CASE
    WHEN capability = '{{"verification":"unverified"}}'::jsonb THEN TRUE
    WHEN jsonb_typeof(capability) = 'object'
     AND capability ?& {_CAPABILITY_KEYS}
     AND capability - {_CAPABILITY_KEYS} = '{{}}'::jsonb
     AND capability->>'verification' = 'measured'
     AND capability->>'delivery' IN ('pushed', 'polled')
     AND capability->>'sequencing' IN ('sequenced', 'unsequenced')
     AND capability->>'edge_preservation' IN ('preserved', 'may_drop')
     AND capability->>'timestamp_source' IN ('device_clock', 'host_receipt')
     AND CASE
         WHEN jsonb_typeof(capability->'max_delivery_delay_seconds') = 'number'
         THEN (capability->>'max_delivery_delay_seconds')::numeric >= 0
         ELSE FALSE
     END
     AND (
         (capability->>'delivery' = 'pushed'
          AND capability->'polling_interval_seconds' = 'null'::jsonb)
         OR
         (capability->>'delivery' = 'polled'
          AND CASE
              WHEN jsonb_typeof(capability->'polling_interval_seconds') = 'number'
              THEN (capability->>'polling_interval_seconds')::numeric > 0
               AND (capability->>'max_delivery_delay_seconds')::numeric
                   >= (capability->>'polling_interval_seconds')::numeric
              ELSE FALSE
          END)
     )
    THEN TRUE
    ELSE FALSE
END
"""

_POINT_TOPOLOGY_FUNCTION = """
CREATE FUNCTION device_assert_point_topology() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    connector_station uuid;
    connector_status text;
    station_status text;
BEGIN
    IF TG_OP = 'INSERT'
       OR NEW.station_id IS DISTINCT FROM OLD.station_id
       OR NEW.connector_id IS DISTINCT FROM OLD.connector_id THEN
        -- 先锁连接器；连接器移动语句已持有同一行锁，因此点位创建与父级移动被串行化。
        SELECT station_id, status::text
          INTO connector_station, connector_status
          FROM device_connector
         WHERE id = NEW.connector_id
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN NEW;
        END IF;
        SELECT status::text INTO station_status
          FROM device_station
         WHERE id = NEW.station_id
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN NEW;
        END IF;
        IF connector_station IS DISTINCT FROM NEW.station_id THEN
            RAISE EXCEPTION 'device_point_connector_station_mismatch' USING ERRCODE = 'P0001';
        END IF;
        IF connector_status <> 'active' THEN
            RAISE EXCEPTION 'device_point_connector_deactivated' USING ERRCODE = 'P0001';
        END IF;
        IF station_status <> 'active' THEN
            RAISE EXCEPTION 'device_point_station_deactivated' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

_POINT_TOPOLOGY_TRIGGER = """
CREATE TRIGGER device_point_topology_must_be_consistent
    BEFORE INSERT OR UPDATE OF station_id, connector_id ON device_point
    FOR EACH ROW EXECUTE FUNCTION device_assert_point_topology()
"""

_CONNECTOR_TOPOLOGY_WITH_POINTS = """
CREATE OR REPLACE FUNCTION device_assert_connector_topology() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target_station_status text;
    target_host_status text;
BEGIN
    IF TG_OP = 'INSERT'
       OR NEW.station_id IS DISTINCT FROM OLD.station_id
       OR NEW.host_id IS DISTINCT FROM OLD.host_id THEN
        IF TG_OP = 'UPDATE'
           AND NEW.station_id IS DISTINCT FROM OLD.station_id
           AND EXISTS (SELECT 1 FROM device_point WHERE connector_id = OLD.id) THEN
            RAISE EXCEPTION 'device_connector_has_points' USING ERRCODE = 'P0001';
        END IF;
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
        ) OR EXISTS (
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

_CONNECTOR_TOPOLOGY_WITHOUT_POINTS = """
CREATE OR REPLACE FUNCTION device_assert_connector_topology() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target_station_status text;
    target_host_status text;
BEGIN
    IF TG_OP = 'INSERT'
       OR NEW.station_id IS DISTINCT FROM OLD.station_id
       OR NEW.host_id IS DISTINCT FROM OLD.host_id THEN
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
        ) OR EXISTS (
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
