"""device：工位、相机及其推理机/后端拓扑。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"

RAW_SQL_TABLES = frozenset(
    {
        "device_camera",
        "device_inference_backend",
        "device_inference_host",
        "device_station",
    }
)


def upgrade() -> None:
    op.create_table(
        "device_station",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=False),
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
        sa.CheckConstraint("char_length(code) > 0", name=op.f("ck_device_station_code_nonempty")),
        sa.CheckConstraint("char_length(name) > 0", name=op.f("ck_device_station_name_nonempty")),
        sa.CheckConstraint(
            "jsonb_typeof(tags) = 'array'", name=op.f("ck_device_station_tags_array")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_station")),
        sa.UniqueConstraint("code", name=op.f("uq_device_station_code")),
    )
    op.create_table(
        "device_camera",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("main_stream_path", sa.String(length=255), nullable=False),
        sa.Column("sub_stream_path", sa.String(length=255), nullable=False),
        sa.Column("credentials_configured", sa.Boolean(), nullable=False),
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("backend_id", sa.Uuid(), nullable=False),
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
        sa.CheckConstraint("char_length(name) > 0", name=op.f("ck_device_camera_name_nonempty")),
        sa.CheckConstraint(
            "char_length(address) > 0", name=op.f("ck_device_camera_address_nonempty")
        ),
        sa.CheckConstraint(
            "char_length(main_stream_path) > 0",
            name=op.f("ck_device_camera_main_stream_path_nonempty"),
        ),
        sa.CheckConstraint(
            "char_length(sub_stream_path) > 0",
            name=op.f("ck_device_camera_sub_stream_path_nonempty"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_device_camera_station_id_device_station"),
        ),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_device_camera_host_id_device_inference_host"),
        ),
        sa.ForeignKeyConstraint(
            ["backend_id"],
            ["device_inference_backend.id"],
            name=op.f("fk_device_camera_backend_id_device_inference_backend"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_camera")),
    )
    op.create_index(op.f("ix_device_camera_station_id"), "device_camera", ["station_id"])
    op.create_index(op.f("ix_device_camera_host_id"), "device_camera", ["host_id"])
    op.create_index(op.f("ix_device_camera_backend_id"), "device_camera", ["backend_id"])
    op.execute(sa.text(_CAMERA_TOPOLOGY_FUNCTION))
    op.execute(sa.text(_CAMERA_TOPOLOGY_TRIGGER))
    op.execute(sa.text(_CAMERA_STATION_TOPOLOGY_FUNCTION))
    op.execute(sa.text(_CAMERA_STATION_TOPOLOGY_TRIGGER))
    op.execute(sa.text(_BACKEND_TOPOLOGY_FUNCTION))
    op.execute(sa.text(_BACKEND_TOPOLOGY_TRIGGER))


_CAMERA_TOPOLOGY_FUNCTION = """
CREATE FUNCTION device_assert_camera_topology() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target_backend_host uuid;
    target_backend_status text;
    target_backend_template uuid;
    target_host_status text;
    target_station_status text;
    binding_changed boolean;
    affected_station uuid;
BEGIN
    binding_changed := TG_OP = 'INSERT'
        OR NEW.station_id IS DISTINCT FROM OLD.station_id
        OR NEW.host_id IS DISTINCT FROM OLD.host_id
        OR NEW.backend_id IS DISTINCT FROM OLD.backend_id;

    IF binding_changed THEN
        -- 拓扑写入统一先锁后端，再按 UUID 升序锁工位；相机跨工位移动时两边都要锁。
        -- 这样相机写入、父后端变更和另一条相机移动不会取得相反的锁序。
        IF TG_OP = 'UPDATE' AND OLD.backend_id IS DISTINCT FROM NEW.backend_id THEN
            IF OLD.backend_id < NEW.backend_id THEN
                PERFORM 1 FROM device_inference_backend
                 WHERE id = OLD.backend_id FOR UPDATE;
                PERFORM 1 FROM device_inference_backend
                 WHERE id = NEW.backend_id FOR UPDATE;
            ELSE
                PERFORM 1 FROM device_inference_backend
                 WHERE id = NEW.backend_id FOR UPDATE;
                PERFORM 1 FROM device_inference_backend
                 WHERE id = OLD.backend_id FOR UPDATE;
            END IF;
        ELSE
            PERFORM 1 FROM device_inference_backend
             WHERE id = NEW.backend_id FOR UPDATE;
        END IF;

        SELECT host_id, status::text, template_version_id
          INTO target_backend_host, target_backend_status, target_backend_template
          FROM device_inference_backend
         WHERE id = NEW.backend_id
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN NEW;
        END IF;

        SELECT status::text INTO target_host_status
          FROM device_inference_host
         WHERE id = NEW.host_id
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN NEW;
        END IF;

        IF TG_OP = 'UPDATE' AND OLD.station_id IS DISTINCT FROM NEW.station_id THEN
            IF OLD.station_id < NEW.station_id THEN
                PERFORM 1 FROM device_station WHERE id = OLD.station_id FOR UPDATE;
                PERFORM 1 FROM device_station WHERE id = NEW.station_id FOR UPDATE;
            ELSE
                PERFORM 1 FROM device_station WHERE id = NEW.station_id FOR UPDATE;
                PERFORM 1 FROM device_station WHERE id = OLD.station_id FOR UPDATE;
            END IF;
        ELSE
            PERFORM 1 FROM device_station WHERE id = NEW.station_id FOR UPDATE;
        END IF;

        SELECT status::text INTO target_station_status
          FROM device_station
         WHERE id = NEW.station_id;
        IF NOT FOUND THEN
            RETURN NEW;
        END IF;

        IF target_backend_host IS DISTINCT FROM NEW.host_id THEN
            RAISE EXCEPTION 'device_camera_host_backend_mismatch' USING ERRCODE = 'P0001';
        END IF;
        IF target_host_status <> 'active' THEN
            RAISE EXCEPTION 'device_camera_host_deactivated' USING ERRCODE = 'P0001';
        END IF;
        IF target_backend_status <> 'active' THEN
            RAISE EXCEPTION 'device_camera_backend_deactivated' USING ERRCODE = 'P0001';
        END IF;
        IF target_station_status <> 'active' THEN
            RAISE EXCEPTION 'device_camera_station_deactivated' USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (
            SELECT 1
              FROM device_camera AS existing
              JOIN device_inference_backend AS existing_backend
                ON existing_backend.id = existing.backend_id
             WHERE existing.station_id = NEW.station_id
               AND existing.id IS DISTINCT FROM NEW.id
               AND existing_backend.template_version_id IS DISTINCT FROM target_backend_template
        ) THEN
            RAISE EXCEPTION 'device_camera_station_template_conflict' USING ERRCODE = 'P0001';
        END IF;
        IF EXISTS (
            SELECT 1 FROM device_camera AS existing
             WHERE existing.station_id = NEW.station_id
               AND existing.id IS DISTINCT FROM NEW.id
               AND existing.host_id IS DISTINCT FROM NEW.host_id
        ) THEN
            RAISE EXCEPTION 'device_camera_station_host_conflict' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

_CAMERA_TOPOLOGY_TRIGGER = """
CREATE TRIGGER device_camera_topology_must_be_consistent
    BEFORE INSERT OR UPDATE OF station_id, host_id, backend_id ON device_camera
    FOR EACH ROW EXECUTE FUNCTION device_assert_camera_topology()
"""

_CAMERA_STATION_TOPOLOGY_FUNCTION = """
CREATE FUNCTION device_assert_station_camera_topology() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    expected_host uuid;
    expected_template uuid;
BEGIN
    -- BEFORE 先锁住父行，AFTER 再核对语句完成后的行集。READ COMMITTED 在锁等待后可能仍
    -- 使用较早的语句快照，不能省掉这层最终状态校验。
    SELECT camera.host_id, backend.template_version_id
      INTO expected_host, expected_template
      FROM device_camera AS camera
      JOIN device_inference_backend AS backend ON backend.id = camera.backend_id
     WHERE camera.station_id = NEW.station_id
     ORDER BY camera.id
     LIMIT 1;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF EXISTS (
        SELECT 1
          FROM device_camera AS camera
         WHERE camera.station_id = NEW.station_id
           AND camera.host_id IS DISTINCT FROM expected_host
    ) THEN
        RAISE EXCEPTION 'device_camera_station_host_conflict' USING ERRCODE = 'P0001';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM device_camera AS camera
          JOIN device_inference_backend AS backend ON backend.id = camera.backend_id
         WHERE camera.station_id = NEW.station_id
           AND backend.template_version_id IS DISTINCT FROM expected_template
    ) THEN
        RAISE EXCEPTION 'device_camera_station_template_conflict' USING ERRCODE = 'P0001';
    END IF;
    RETURN NULL;
END;
$$
"""

_CAMERA_STATION_TOPOLOGY_TRIGGER = """
CREATE TRIGGER device_camera_station_topology_must_be_consistent
    AFTER INSERT OR UPDATE OF station_id, host_id, backend_id ON device_camera
    FOR EACH ROW EXECUTE FUNCTION device_assert_station_camera_topology()
"""

_BACKEND_TOPOLOGY_FUNCTION = """
CREATE FUNCTION device_assert_backend_topology() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    affected_station uuid;
BEGIN
    IF NEW.host_id IS DISTINCT FROM OLD.host_id
       OR NEW.template_version_id IS DISTINCT FROM OLD.template_version_id THEN
        -- UPDATE 已持有后端行锁；这里沿用相机写入的顺序，再按 UUID 升序锁受影响工位。
        FOR affected_station IN
            SELECT DISTINCT camera.station_id
              FROM device_camera AS camera
             WHERE camera.backend_id = OLD.id
             ORDER BY camera.station_id
        LOOP
            PERFORM 1 FROM device_station
             WHERE id = affected_station FOR UPDATE;
        END LOOP;

        IF NEW.host_id IS DISTINCT FROM OLD.host_id
           AND EXISTS (
               SELECT 1
                 FROM device_camera AS camera
                WHERE camera.backend_id = OLD.id
           ) THEN
            RAISE EXCEPTION 'device_camera_host_backend_mismatch' USING ERRCODE = 'P0001';
        END IF;

        IF NEW.template_version_id IS DISTINCT FROM OLD.template_version_id
           AND EXISTS (
               SELECT 1
                 FROM device_camera AS camera
                 JOIN device_inference_backend AS backend
                   ON backend.id = camera.backend_id
                WHERE camera.station_id IN (
                    SELECT affected_camera.station_id
                      FROM device_camera AS affected_camera
                     WHERE affected_camera.backend_id = OLD.id
                )
                  AND (
                      CASE
                          WHEN camera.backend_id = OLD.id THEN NEW.template_version_id
                          ELSE backend.template_version_id
                      END
                  ) IS DISTINCT FROM NEW.template_version_id
           ) THEN
            -- 比较的是模板变更后的工位最终状态，NULL 与 NULL 仍表示一致。
            RAISE EXCEPTION 'device_camera_station_template_conflict' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

_BACKEND_TOPOLOGY_TRIGGER = """
CREATE TRIGGER device_backend_topology_must_be_consistent
    BEFORE UPDATE OF host_id, template_version_id ON device_inference_backend
    FOR EACH ROW EXECUTE FUNCTION device_assert_backend_topology()
"""


def downgrade() -> None:
    op.execute(
        sa.text("DROP TRIGGER device_camera_station_topology_must_be_consistent ON device_camera")
    )
    op.execute(sa.text("DROP TRIGGER device_camera_topology_must_be_consistent ON device_camera"))
    op.execute(
        sa.text(
            "DROP TRIGGER device_backend_topology_must_be_consistent ON device_inference_backend"
        )
    )
    op.execute(sa.text("DROP FUNCTION device_assert_station_camera_topology()"))
    op.execute(sa.text("DROP FUNCTION device_assert_backend_topology()"))
    op.execute(sa.text("DROP FUNCTION device_assert_camera_topology()"))
    op.drop_index(op.f("ix_device_camera_backend_id"), table_name="device_camera")
    op.drop_index(op.f("ix_device_camera_host_id"), table_name="device_camera")
    op.drop_index(op.f("ix_device_camera_station_id"), table_name="device_camera")
    op.drop_table("device_camera")
    op.drop_table("device_station")
