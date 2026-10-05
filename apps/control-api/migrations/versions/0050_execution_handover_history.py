"""execution：强制改绑历史不随设备删除级联消失。

0048 把 `execution_handover` 的工位/旧机/目标机外键建成 `ON DELETE CASCADE`：删掉一台
推理机或工位会静默抹掉两人确认过的强制改绑历史。强制改绑是唯一可能同时驱动两套物理执行器
的操作，其两名确认人是必须留存的诊断事实（§5.17），不能被一次设备 CRUD 删除连带清除。

本迁移只替换这三个外键的删除行为为 `RESTRICT`：列、值、索引与内容都不动；删除仍引用历史的
设备由 `device` 映射成稳定的 409 拒绝。旧 0048/0049 不改写，回滚按原 `CASCADE` 重建且不删历史。
"""

from __future__ import annotations

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"


def upgrade() -> None:
    _replace_foreign_keys("RESTRICT")


def downgrade() -> None:
    _replace_foreign_keys("CASCADE")


def _replace_foreign_keys(ondelete: str) -> None:
    """把三个引用外键的删除行为替换为 `ondelete`，保留列、值与索引。"""
    op.drop_constraint(
        op.f("fk_execution_handover_station_id_device_station"),
        "execution_handover",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_execution_handover_station_id_device_station"),
        "execution_handover",
        "device_station",
        ["station_id"],
        ["id"],
        ondelete=ondelete,
    )
    op.drop_constraint(
        op.f("fk_execution_handover_from_host_id_device_inference_host"),
        "execution_handover",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_execution_handover_from_host_id_device_inference_host"),
        "execution_handover",
        "device_inference_host",
        ["from_host_id"],
        ["id"],
        ondelete=ondelete,
    )
    op.drop_constraint(
        op.f("fk_execution_handover_to_host_id_device_inference_host"),
        "execution_handover",
        type_="foreignkey",
    )
    op.create_foreign_key(
        op.f("fk_execution_handover_to_host_id_device_inference_host"),
        "execution_handover",
        "device_inference_host",
        ["to_host_id"],
        ["id"],
        ondelete=ondelete,
    )
