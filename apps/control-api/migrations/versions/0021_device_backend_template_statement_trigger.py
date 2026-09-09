"""
把多后端模板切换的最终一致性校验从逐行 BEFORE trigger 移到语句级 AFTER trigger。

0007 的相机写入仍然使用后端到工位的锁序；本迁移只替换后端模板校验，避免一次合法的
批量 UPDATE 在第一行看到其余后端的旧模板。transition table 让校验读取语句完成后的
后端集合，仓储则在更新前显式按稳定顺序取得同一组锁。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"

RAW_SQL_TABLES = frozenset(
    {
        "device_camera",
        "device_inference_backend",
        "device_station",
    }
)

_BACKEND_TOPOLOGY_FUNCTION = """
CREATE OR REPLACE FUNCTION device_assert_backend_topology() RETURNS trigger
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

_BACKEND_TEMPLATE_STATEMENT_FUNCTION = """
CREATE FUNCTION device_assert_backend_template_topology() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    affected_station uuid;
    expected_template uuid;
BEGIN
    FOR affected_station IN
        SELECT DISTINCT camera.station_id
          FROM device_camera AS camera
          JOIN old_backends AS old_backend ON old_backend.id = camera.backend_id
          JOIN new_backends AS new_backend ON new_backend.id = old_backend.id
         WHERE old_backend.template_version_id IS DISTINCT FROM new_backend.template_version_id
         ORDER BY camera.station_id
    LOOP
        SELECT backend.template_version_id
          INTO expected_template
          FROM device_camera AS camera
          JOIN device_inference_backend AS backend ON backend.id = camera.backend_id
         WHERE camera.station_id = affected_station
         ORDER BY camera.id
         LIMIT 1;

        IF EXISTS (
            SELECT 1
              FROM device_camera AS camera
              JOIN device_inference_backend AS backend
                ON backend.id = camera.backend_id
             WHERE camera.station_id = affected_station
               AND backend.template_version_id IS DISTINCT FROM expected_template
        ) THEN
            RAISE EXCEPTION 'device_camera_station_template_conflict' USING ERRCODE = 'P0001';
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$
"""

_BACKEND_TEMPLATE_STATEMENT_TRIGGER = """
CREATE TRIGGER device_backend_template_topology_must_be_consistent
    AFTER UPDATE ON device_inference_backend
    REFERENCING OLD TABLE AS old_backends NEW TABLE AS new_backends
    FOR EACH STATEMENT EXECUTE FUNCTION device_assert_backend_template_topology()
"""


def upgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER device_backend_topology_must_be_consistent ON device_inference_backend"
        )
    )
    op.execute(sa.text(_BACKEND_TOPOLOGY_FUNCTION))
    op.execute(sa.text(_BACKEND_TOPOLOGY_TRIGGER))
    op.execute(sa.text(_BACKEND_TEMPLATE_STATEMENT_FUNCTION))
    op.execute(sa.text(_BACKEND_TEMPLATE_STATEMENT_TRIGGER))


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER device_backend_template_topology_must_be_consistent "
            "ON device_inference_backend"
        )
    )
    op.execute(sa.text("DROP FUNCTION device_assert_backend_template_topology()"))
    op.execute(
        sa.text(
            "DROP TRIGGER device_backend_topology_must_be_consistent ON device_inference_backend"
        )
    )
    op.execute(sa.text(_LEGACY_BACKEND_TOPOLOGY_FUNCTION))
    op.execute(sa.text(_BACKEND_TOPOLOGY_TRIGGER))


_LEGACY_BACKEND_TOPOLOGY_FUNCTION = """
CREATE OR REPLACE FUNCTION device_assert_backend_topology() RETURNS trigger
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
