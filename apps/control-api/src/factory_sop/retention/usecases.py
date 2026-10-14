"""retention 用例与 seam：retention 拥有全局策略与工位整组覆盖记录（§5.19）。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.retention.errors import RetentionRefusalCode, RetentionRefusedError
from factory_sop.retention.model import (
    DEFAULT_RETENTION_POLICY,
    RetentionPolicy,
    RetentionPolicyMode,
    RetentionPolicyState,
    check_retention_selection,
    resolve_retention_policy,
)
from nvsop_contracts import ResolvedRetentionPolicy


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


@dataclass(frozen=True, slots=True)
class StationRetentionRecord:
    """retention 拥有的工位覆盖记录：整组自定义，或明确整组跟随全局默认。"""

    station_id: UUID
    mode: RetentionPolicyMode
    overrides: RetentionPolicy | None
    revision: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("station retention revision must be a positive integer")
        check_retention_selection(mode=self.mode, overrides=self.overrides)


class StationRetentionOverrideRepository(Protocol):
    """工位覆盖记录的读写 seam；方法都不提交事务，写入都是版本条件写。"""

    def get(self, station_id: UUID) -> StationRetentionRecord | None:
        """该工位尚无记录时返回 ``None``（它整组跟随全局默认）。"""
        ...

    def insert_if_absent(self, record: StationRetentionRecord) -> bool:
        """仅在该工位尚无记录时插入；已有记录时返回 ``False``，不覆盖。"""
        ...

    def replace_if_current(self, *, expected_revision: int, record: StationRetentionRecord) -> bool:
        """仅在该工位记录版本仍等于 expected 时整组替换。"""
        ...


class RetentionStationPresence(StrEnum):
    """工位对 retention 可见的存在与启用事实。"""

    MISSING = "missing"
    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class StationFactsGateway(Protocol):
    """retention 读取工位存在/启用事实所用的 device owner 最小 seam。"""

    def station_presence(self, station_id: UUID) -> RetentionStationPresence:
        """返回工位是否存在与是否启用；不携带任何保留配置。"""
        ...


@dataclass(frozen=True, slots=True)
class StationRetentionConfiguration:
    """工位保留的默认组、覆盖组与唯一生效组；不逐字段混合。"""

    station_id: UUID
    revision: int
    mode: RetentionPolicyMode
    default: RetentionPolicy
    overrides: RetentionPolicy | None
    effective: RetentionPolicy


@dataclass(frozen=True, slots=True)
class StationRetentionProjection:
    """配置组合层可见的最小保留事实；生效组已是机器契约形状。"""

    station_id: UUID
    revision: int
    effective: ResolvedRetentionPolicy


class StationRetentionProjectionGateway(Protocol):
    """机器配置组合只可通过的 retention owner 接缝。"""

    def for_station(self, station_id: UUID) -> StationRetentionProjection:
        """返回该工位覆盖记录版本与中心解析后的完整生效组。"""
        ...


def default_retention_policy(policies: RetentionPolicyRepository) -> RetentionPolicy:
    """返回持久化全局策略；尚未持久化时返回代码默认值。"""
    persisted = policies.get_global()
    return DEFAULT_RETENTION_POLICY if persisted is None else persisted.policy


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


def read_station_retention(
    caller: Caller,
    station_id: UUID,
    *,
    policies: RetentionPolicyRepository,
    overrides: StationRetentionOverrideRepository,
    stations: StationFactsGateway,
) -> StationRetentionConfiguration:
    """读取全局默认、工位覆盖与唯一生效组；只读，不写入。"""
    authorize(caller, Permission.RETENTION_POLICY_VIEW)
    _require_present_station(stations, station_id)
    return _station_retention(station_id, policies, overrides.get(station_id))


def update_station_retention(
    caller: Caller,
    station_id: UUID,
    *,
    mode: RetentionPolicyMode,
    overrides: RetentionPolicy | None,
    expected_revision: int,
    policies: RetentionPolicyRepository,
    records: StationRetentionOverrideRepository,
    stations: StationFactsGateway,
) -> StationRetentionConfiguration:
    """整组替换工位保留覆盖；不合规、工位不可用或版本冲突时拒绝，不删除任何数据。

    ``expected_revision`` 是该工位覆盖记录自身的版本：``0`` 表示首次建立，写入走真实 CAS
    （并发下只有一个请求能建立记录），其余情况是条件更新，丢弃更新请求会收到冲突。
    本用例不提交事务：提交由请求作用域 Unit of Work（ADR-0002）或调用方负责。
    """
    authorize(caller, Permission.RETENTION_POLICY_EDIT)
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")
    # 先按纯规则拒绝不配对的选择，拒绝时不触碰任何记录。
    try:
        check_retention_selection(mode=mode, overrides=overrides)
    except ValueError as error:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_INVALID) from error
    _require_active_station(stations, station_id)
    record = StationRetentionRecord(
        station_id=station_id,
        mode=mode,
        overrides=overrides,
        revision=expected_revision + 1,
    )
    written = (
        records.insert_if_absent(record)
        if expected_revision == 0
        else records.replace_if_current(expected_revision=expected_revision, record=record)
    )
    if not written:
        raise RetentionPolicyConflictError(expected_revision)
    return _station_retention(station_id, policies, record)


def _require_present_station(stations: StationFactsGateway, station_id: UUID) -> None:
    if stations.station_presence(station_id) is RetentionStationPresence.MISSING:
        raise RetentionRefusedError(RetentionRefusalCode.STATION_NOT_FOUND)


def _require_active_station(stations: StationFactsGateway, station_id: UUID) -> None:
    presence = stations.station_presence(station_id)
    if presence is RetentionStationPresence.MISSING:
        raise RetentionRefusedError(RetentionRefusalCode.STATION_NOT_FOUND)
    if presence is RetentionStationPresence.DEACTIVATED:
        raise RetentionRefusedError(RetentionRefusalCode.STATION_DEACTIVATED)


def _station_retention(
    station_id: UUID,
    policies: RetentionPolicyRepository,
    record: StationRetentionRecord | None,
) -> StationRetentionConfiguration:
    default = default_retention_policy(policies)
    mode = RetentionPolicyMode.FOLLOW_GLOBAL if record is None else record.mode
    overrides = None if record is None else record.overrides
    return StationRetentionConfiguration(
        station_id=station_id,
        revision=0 if record is None else record.revision,
        mode=mode,
        default=default,
        overrides=overrides,
        effective=resolve_retention_policy(default, mode=mode, overrides=overrides),
    )


__all__ = [
    "RetentionPolicyConflictError",
    "RetentionPolicyRepository",
    "RetentionStationPresence",
    "StationFactsGateway",
    "StationRetentionConfiguration",
    "StationRetentionOverrideRepository",
    "StationRetentionProjection",
    "StationRetentionProjectionGateway",
    "StationRetentionRecord",
    "default_retention_policy",
    "get_policy",
    "read_station_retention",
    "update_policy",
    "update_station_retention",
]
