"""device: 录像压缩控制复用委托队列, 只追加明确的动作与观测结果。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None
RAW_SQL_TABLES = frozenset({"device_pending_command"})


def upgrade() -> None:
    op.execute(
        "ALTER TYPE device_pending_command_type "
        "ADD VALUE IF NOT EXISTS 'control_recording_compression'"
    )
    op.add_column(
        "device_pending_command", sa.Column("compression_action", sa.String(16), nullable=True)
    )
    op.add_column(
        "device_pending_command", sa.Column("compression_result", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("device_pending_command", "compression_result")
    op.drop_column("device_pending_command", "compression_action")
