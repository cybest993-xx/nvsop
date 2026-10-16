"""device：为正常交权停机命令登记显式 command type。"""

from __future__ import annotations

from alembic import op

revision = "0071"
down_revision: str | None = "0070"
branch_labels = None
depends_on = None

RAW_SQL_TABLES = frozenset({"device_pending_command"})


def upgrade() -> None:
    op.execute(
        "ALTER TYPE device_pending_command_type ADD VALUE IF NOT EXISTS 'stop_execution_handover'"
    )


def downgrade() -> None:
    # PostgreSQL 不支持删除单个枚举值；保留值不改变已存在命令的语义。
    pass
