"""device：委托命令类型增加再切片。

`device_pending_command_type` 增加 `reclip_evidence`；命令仍在同一队列表，领取按命令类型
收窄，故 S038/S039 就绪前旧严格消费者不会取到它。Postgres 不能删除单个枚举值，降级留值。
"""

from __future__ import annotations

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"

# op.execute 的 ALTER TYPE 目标枚举属于 device_pending_command，按拥有者声明。
RAW_SQL_TABLES = frozenset({"device_pending_command"})


def upgrade() -> None:
    op.execute("ALTER TYPE device_pending_command_type ADD VALUE IF NOT EXISTS 'reclip_evidence'")


def downgrade() -> None:
    # Postgres 没有删除单个枚举值的 DDL；保留该值不改变既有行的语义。
    pass
