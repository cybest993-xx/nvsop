"""monitor: 聚合归档、扫描位置和明细回收后的幂等身份。"""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (
        "monitor_decision_identity",
        "monitor_observation_identity",
        "monitor_health_identity",
    ):
        op.add_column(table, sa.Column("retired_payload_sha256", sa.String(64), nullable=True))
        op.add_column(
            table,
            sa.Column("retention_archived", sa.Boolean(), nullable=False, server_default="false"),
        )
    op.create_table(
        "monitor_record_archive",
        sa.Column("archive_id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("station_id", sa.String(128), nullable=True),
        sa.Column("host_id", sa.String(128), nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        *(
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0")
            for name in ("decisions", "observations", "health", "disposals")
        ),
    )
    for name in ("station_id", "host_id", "bucket_start"):
        op.create_index(f"ix_monitor_record_archive_{name}", "monitor_record_archive", [name])
    op.create_table(
        "monitor_retention_cursor",
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("until_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("until_event_id", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("monitor_retention_cursor")
    op.drop_table("monitor_record_archive")
    for table in (
        "monitor_decision_identity",
        "monitor_observation_identity",
        "monitor_health_identity",
    ):
        op.drop_column(table, "retention_archived")
        op.drop_column(table, "retired_payload_sha256")
