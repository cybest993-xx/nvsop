"""execution：中心工位物理执行权当前租约。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"

RAW_SQL_TABLES = frozenset({"execution_station_grant"})

_ACTIVE_GRANT_DELETE_FUNCTION = """
CREATE FUNCTION execution_reject_active_grant_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.lease_expires_at > clock_timestamp() THEN
        RAISE EXCEPTION USING
            ERRCODE = 'P0001',
            MESSAGE = 'execution_active_grant_delete';
    END IF;
    RETURN OLD;
END;
$$
"""

_ACTIVE_GRANT_DELETE_TRIGGER = """
CREATE TRIGGER execution_active_grant_delete_guard
    BEFORE DELETE ON execution_station_grant
    FOR EACH ROW EXECUTE FUNCTION execution_reject_active_grant_delete()
"""


def upgrade() -> None:
    op.create_table(
        "execution_station_grant",
        sa.Column("station_id", sa.Uuid(), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("holder_host_id", sa.Uuid(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "lease_expires_at = renewed_at + INTERVAL '7 days'",
            name=op.f("ck_execution_station_grant_seven_day_ttl"),
        ),
        sa.ForeignKeyConstraint(
            ["station_id"],
            ["device_station.id"],
            name=op.f("fk_execution_station_grant_station_id_device_station"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["holder_host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_execution_station_grant_holder_host_id_device_inference_host"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("station_id", name=op.f("pk_execution_station_grant")),
        sa.UniqueConstraint("grant_id", name=op.f("uq_execution_station_grant_grant_id")),
    )
    op.create_index(
        op.f("ix_execution_station_grant_holder_host_id"),
        "execution_station_grant",
        ["holder_host_id"],
    )
    op.execute(sa.text(_ACTIVE_GRANT_DELETE_FUNCTION))
    op.execute(sa.text(_ACTIVE_GRANT_DELETE_TRIGGER))


def downgrade() -> None:
    op.execute(
        sa.text("DROP TRIGGER execution_active_grant_delete_guard ON execution_station_grant")
    )
    op.execute(sa.text("DROP FUNCTION execution_reject_active_grant_delete()"))
    op.drop_index(
        op.f("ix_execution_station_grant_holder_host_id"),
        table_name="execution_station_grant",
    )
    op.drop_table("execution_station_grant")
