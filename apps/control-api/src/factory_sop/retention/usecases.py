"""retention 用例与仓储 seam：读取/更新全局保留策略，只写策略，不删除数据（§5.19）。"""

from __future__ import annotations

from typing import Protocol

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.retention.model import (
    DEFAULT_RETENTION_POLICY,
    RetentionPolicy,
    RetentionPolicyState,
)


class RetentionPolicyRepository(Protocol):
    """全局保留策略的读写 seam；方法都不提交事务。"""

    def get_global(self) -> RetentionPolicyState | None:
        """尚未持久化时返回 ``None``（由调用方回退代码默认值）。"""
        ...

    def replace_if_current(self, *, expected_revision: int, value: RetentionPolicyState) -> bool:
        """仅在当前 revision 等于 expected 时写入；0 表示首次插入。"""
        ...


class RetentionPolicyConflictError(Exception):
    def __init__(self, expected_revision: int) -> None:
        super().__init__(f"retention policy revision changed; expected {expected_revision}")
        self.expected_revision = expected_revision


def get_policy(caller: Caller, repository: RetentionPolicyRepository) -> RetentionPolicyState:
    """返回全局策略；尚未持久化时返回代码默认值（revision 0），不写入。"""
    authorize(caller, Permission.RETENTION_POLICY_VIEW)
    state = repository.get_global()
    return state if state is not None else RetentionPolicyState(DEFAULT_RETENTION_POLICY, 0)


def update_policy(
    caller: Caller,
    policy: RetentionPolicy,
    *,
    expected_revision: int,
    repository: RetentionPolicyRepository,
) -> RetentionPolicyState:
    """整组替换全局策略；乐观锁冲突拒绝，且只改变未来回收依据，不同步删除对象。

    本用例不提交事务：提交由请求作用域 Unit of Work（ADR-0002）或调用方负责。
    """
    authorize(caller, Permission.RETENTION_POLICY_EDIT)
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")
    value = RetentionPolicyState(policy=policy, revision=expected_revision + 1)
    if not repository.replace_if_current(expected_revision=expected_revision, value=value):
        raise RetentionPolicyConflictError(expected_revision)
    return value


__all__ = [
    "RetentionPolicyConflictError",
    "RetentionPolicyRepository",
    "get_policy",
    "update_policy",
]
