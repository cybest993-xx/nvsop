"""device: the two tables of the C4.1 slice, and the active-host rule on backend writes

Revision ID: 0005
Revises: 0004

The trigger at the bottom holds the database half of "a new backend binding hangs off an
ACTIVE host": the use cases refuse a deactivated host, the foreign key refuses a missing one,
and this trigger refuses a deactivated one for whoever writes the table directly. Existing
backend rows remain historical references when a host is later 停用, so editing an endpoint
without moving it is still allowed. No declarative constraint can say it — backends keep
referencing a host that is LATER deactivated, which is exactly what 停用 requires — so the
check runs at insert or when `host_id` actually changes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"

# The tables this migration's raw SQL touches — the declaration of record for the
# migration-ownership checker, which verifies each carries this migration's module prefix.
RAW_SQL_TABLES = frozenset({"device_inference_backend", "device_inference_host"})


def upgrade() -> None:
    op.create_table(
        "device_inference_host",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("mediamtx_address", sa.String(length=255), nullable=True),
        sa.Column("recording_window_seconds", sa.BigInteger(), nullable=False),
        sa.Column("disk_watermark_percent", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "recording_window_seconds > 0",
            name=op.f("ck_device_inference_host_recording_window_positive"),
        ),
        sa.CheckConstraint(
            "disk_watermark_percent BETWEEN 1 AND 99",
            name=op.f("ck_device_inference_host_disk_watermark_percent_range"),
        ),
        sa.Column(
            "status",
            sa.Enum("active", "deactivated", name="device_status"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_inference_host")),
        sa.UniqueConstraint("name", name=op.f("uq_device_inference_host_name")),
    )
    op.create_table(
        "device_inference_backend",
        # The shared `device_status` type was created by the table above; this column
        # carries it without creating it a second time.
        sa.Column(
            "status",
            sa.Enum("active", "deactivated", name="device_status", create_type=False),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("host_id", sa.Uuid(), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=False),
        sa.Column("template_version_id", sa.Uuid(), nullable=True),
        sa.Column(
            "connection_state",
            sa.Enum("unverified", "success", "failure", name="device_connection_state"),
            nullable=False,
        ),
        sa.Column("connection_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connection_detail", sa.String(length=255), nullable=True),
        sa.Column("self_reported_model_ids", postgresql.JSONB(), nullable=True),
        sa.Column("self_reported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("updated_by", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["host_id"],
            ["device_inference_host.id"],
            name=op.f("fk_device_inference_backend_host_id_device_inference_host"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_inference_backend")),
        sa.UniqueConstraint(
            "host_id", "base_url", name=op.f("uq_device_inference_backend_host_id_base_url")
        ),
    )
    op.create_index(
        op.f("ix_device_inference_backend_host_id"),
        "device_inference_backend",
        ["host_id"],
        unique=False,
    )
    for statement in (
        _ASSERT_HOST_ACTIVE_FUNCTION,
        _ASSERT_HOST_ACTIVE_TRIGGER,
    ):
        op.execute(sa.text(statement))


# The rule the trigger enforces, and the marker the adapter translates into
# `INFERENCE_HOST_DEACTIVATED`. One statement per object, so the migration-ownership
# checker's declared-target rule (RAW_SQL_TABLES below) reads exactly what runs.
_ASSERT_HOST_ACTIVE_FUNCTION = """
CREATE FUNCTION device_assert_backend_host_is_active() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.host_id IS DISTINCT FROM OLD.host_id THEN
        IF (SELECT status FROM device_inference_host WHERE id = NEW.host_id) <> 'active' THEN
            RAISE EXCEPTION 'device_backend_host_deactivated' USING ERRCODE = 'P0001';
        END IF;
    END IF;
    RETURN NEW;
END;
$$
"""

_ASSERT_HOST_ACTIVE_TRIGGER = """
CREATE TRIGGER device_inference_backend_host_must_be_active
    BEFORE INSERT OR UPDATE OF host_id ON device_inference_backend
    FOR EACH ROW EXECUTE FUNCTION device_assert_backend_host_is_active()
"""


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER device_inference_backend_host_must_be_active ON device_inference_backend"
        )
    )
    op.execute(sa.text("DROP FUNCTION device_assert_backend_host_is_active()"))
    op.drop_index(
        op.f("ix_device_inference_backend_host_id"), table_name="device_inference_backend"
    )
    op.drop_table("device_inference_backend")
    op.drop_table("device_inference_host")
    sa.Enum(name="device_status").drop(op.get_bind())
    sa.Enum(name="device_connection_state").drop(op.get_bind())
