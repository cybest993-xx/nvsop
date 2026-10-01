"""retention 的跨模块接缝：暴露保留类别、策略值、纯规则与用例（§5.19）。

#204 在此扩展工位覆盖与生效解析；#206 经 `category_for` 接入锁存违规事实；#207 由数据 owner
按本策略执行回收。本阶段不接 HTTP/UI。
"""

from __future__ import annotations

from factory_sop.retention.model import (
    DEFAULT_RETENTION_POLICY,
    RetentionCategory,
    RetentionMode,
    RetentionPolicy,
    RetentionPolicyState,
    RetentionRule,
    category_for,
    check_reslice_window,
)
from factory_sop.retention.usecases import (
    RetentionPolicyConflictError,
    RetentionPolicyRepository,
    get_policy,
    update_policy,
)

__all__ = [
    "DEFAULT_RETENTION_POLICY",
    "RetentionCategory",
    "RetentionMode",
    "RetentionPolicy",
    "RetentionPolicyConflictError",
    "RetentionPolicyRepository",
    "RetentionPolicyState",
    "RetentionRule",
    "category_for",
    "check_reslice_window",
    "get_policy",
    "update_policy",
]
