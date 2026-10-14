"""retention 的跨模块接缝：保留类别、策略值、纯规则与用例（§5.19）。

retention 拥有全局默认策略与每工位的整组覆盖记录：本文件暴露工位事实 seam、覆盖记录 seam 与配置
组合使用的生效组投影，其他模块只经此接缝访问，不读 retention 的表。#206 经 `category_for` 接入
锁存违规事实；#207 由数据 owner 按本策略执行回收。
"""

from __future__ import annotations

from factory_sop.retention.model import (
    DEFAULT_RETENTION_POLICY,
    RetentionCategory,
    RetentionMode,
    RetentionPolicy,
    RetentionPolicyMode,
    RetentionPolicyState,
    RetentionRule,
    category_for,
    check_reslice_window,
    check_retention_selection,
    resolve_retention_policy,
)
from factory_sop.retention.usecases import (
    RetentionPolicyConflictError,
    RetentionPolicyRepository,
    RetentionStationPresence,
    StationFactsGateway,
    StationRetentionConfiguration,
    StationRetentionOverrideRepository,
    StationRetentionProjection,
    StationRetentionProjectionGateway,
    StationRetentionRecord,
    default_retention_policy,
    get_policy,
    read_station_retention,
    update_policy,
    update_station_retention,
)

__all__ = [
    "DEFAULT_RETENTION_POLICY",
    "RetentionCategory",
    "RetentionMode",
    "RetentionPolicy",
    "RetentionPolicyConflictError",
    "RetentionPolicyMode",
    "RetentionPolicyRepository",
    "RetentionPolicyState",
    "RetentionRule",
    "RetentionStationPresence",
    "StationFactsGateway",
    "StationRetentionConfiguration",
    "StationRetentionOverrideRepository",
    "StationRetentionProjection",
    "StationRetentionProjectionGateway",
    "StationRetentionRecord",
    "category_for",
    "check_reslice_window",
    "check_retention_selection",
    "default_retention_policy",
    "get_policy",
    "read_station_retention",
    "resolve_retention_policy",
    "update_policy",
    "update_station_retention",
]
