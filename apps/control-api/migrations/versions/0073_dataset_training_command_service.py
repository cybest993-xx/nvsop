"""dataset：训练命令表按训练服务区分，承载 VLM 与已派发取消事实。

同一种训练命令（DDM / VLM）共用一张表与同一套派发状态机：表名改为服务中性的
``dataset_training_command``，新增 ``service`` 维度并把它并入幂等键与在途保护索引；
``cancel_requested_*`` / ``cancel_response`` / ``cancel_detail`` 记录已派发命令的取消请求、
基座原取消响应与本次核实结论。

迁移前写入的行全部属于 DDM（当时只有 DDM 训练命令），回填为 ``ddm``。

约束名从目录读取而不是写成字面量：旧外键名超过 PostgreSQL 的 63 字节标识符上限，
落库时已被截断，迁移不应复制这处截断细节。

回退到单服务的旧 schema 无法表示已写入的 VLM 命令，也无法保证
``(dataset_id, idempotency_key)`` 唯一；此时显式失败，而不是静默删除或改写命令事实。

Revision ID: 0073
Revises: 0072
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0073"
down_revision: str | None = "0072"

# 约束/索引改名、既有行回填都属于写入：目标无法从 Alembic 调用读出，按检查器要求显式声明。
RAW_SQL_TABLES = frozenset({"dataset_training_command"})

_OLD_TABLE = "dataset_ddm_training_command"
_TABLE = "dataset_training_command"


def upgrade() -> None:
    op.rename_table(_OLD_TABLE, _TABLE)
    # 约束名从目录读取：旧外键名超过 63 字节上限，落库时已被 PostgreSQL 截断。
    op.execute(
        """
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'dataset_training_command'::regclass
          AND conname LIKE '%dataset_ddm_training_command%'
    LOOP
        EXECUTE format(
            'ALTER TABLE dataset_training_command RENAME CONSTRAINT %I TO %I',
            item.conname,
            replace(item.conname, 'dataset_ddm_training_command', 'dataset_training_command')
        );
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_class WHERE relname = 'ix_dataset_ddm_training_command_dataset_id'
    ) THEN
        ALTER INDEX ix_dataset_ddm_training_command_dataset_id
            RENAME TO ix_dataset_training_command_dataset_id;
    END IF;
END
$$
        """
    )
    # 幂等键与在途保护都要加入服务维度：先把旧索引/唯一键换成带 service 的版本。
    op.drop_index(f"uq_{_OLD_TABLE}_backend_target_inflight", table_name=_TABLE)
    op.drop_constraint(op.f(f"uq_{_TABLE}_dataset_id_idempotency_key"), _TABLE, type_="unique")
    op.add_column(_TABLE, sa.Column("service", sa.String(length=16), nullable=True))
    op.execute(f"UPDATE {_TABLE} SET service = 'ddm' WHERE service IS NULL")
    op.alter_column(_TABLE, "service", nullable=False)
    op.create_index(op.f(f"ix_{_TABLE}_service"), _TABLE, ["service"])
    op.create_unique_constraint(
        op.f(f"uq_{_TABLE}_dataset_id_service_idempotency_key"),
        _TABLE,
        ["dataset_id", "service", "idempotency_key"],
    )
    op.create_index(
        f"uq_{_TABLE}_service_backend_target_inflight",
        _TABLE,
        ["service", "backend_target"],
        unique=True,
        postgresql_where=sa.text("status IN ('result_unknown', 'bound')"),
    )
    op.add_column(_TABLE, sa.Column("cancel_requested_by", sa.Uuid(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("cancel_response", JSONB(), nullable=True))
    op.add_column(_TABLE, sa.Column("cancel_detail", sa.String(length=512), nullable=True))


def downgrade() -> None:
    # 旧 schema 不能表示 VLM 命令，也不能保留按服务维度的幂等键：有非 DDM 行时先失败，
    # 保证回退不会删除或改建已写入的命令事实。
    op.execute(
        """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM dataset_training_command WHERE service <> 'ddm') THEN
        RAISE EXCEPTION
            'dataset_training_command holds non-DDM training commands; '
            'the pre-0073 schema cannot represent them';
    END IF;
END
$$
        """
    )
    op.drop_column(_TABLE, "cancel_detail")
    op.drop_column(_TABLE, "cancel_response")
    op.drop_column(_TABLE, "cancel_requested_at")
    op.drop_column(_TABLE, "cancel_requested_by")
    op.drop_index(f"uq_{_TABLE}_service_backend_target_inflight", table_name=_TABLE)
    op.drop_constraint(
        op.f(f"uq_{_TABLE}_dataset_id_service_idempotency_key"), _TABLE, type_="unique"
    )
    op.drop_index(op.f(f"ix_{_TABLE}_service"), table_name=_TABLE)
    op.drop_column(_TABLE, "service")
    op.create_unique_constraint(
        op.f(f"uq_{_TABLE}_dataset_id_idempotency_key"),
        _TABLE,
        ["dataset_id", "idempotency_key"],
    )
    op.create_index(
        f"uq_{_OLD_TABLE}_backend_target_inflight",
        _TABLE,
        ["backend_target"],
        unique=True,
        postgresql_where=sa.text("status IN ('result_unknown', 'bound')"),
    )
    op.rename_table(_TABLE, _OLD_TABLE)
    op.execute(
        """
DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'dataset_ddm_training_command'::regclass
          AND conname LIKE '%dataset_training_command%'
    LOOP
        EXECUTE format(
            'ALTER TABLE dataset_ddm_training_command RENAME CONSTRAINT %I TO %I',
            item.conname,
            replace(item.conname, 'dataset_training_command', 'dataset_ddm_training_command')
        );
    END LOOP;
    IF EXISTS (
        SELECT 1 FROM pg_class WHERE relname = 'ix_dataset_training_command_dataset_id'
    ) THEN
        ALTER INDEX ix_dataset_training_command_dataset_id
            RENAME TO ix_dataset_ddm_training_command_dataset_id;
    END IF;
END
$$
        """
    )
