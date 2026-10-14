"""retention 用例与 seam：retention 拥有全局策略、工位整组覆盖记录与变更影响估算（§5.19）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, assert_never
from uuid import UUID

from factory_sop.auth.api import Caller, Permission, authorize
from factory_sop.retention.errors import RetentionRefusalCode, RetentionRefusedError
from factory_sop.retention.model import (
    DEFAULT_RETENTION_POLICY,
    FIELD_IMPACT,
    EdgeImpactSource,
    ImpactAggregate,
    RetentionCategory,
    RetentionFactOwner,
    RetentionFieldImpact,
    RetentionImpactBreakdown,
    RetentionImpactEstimate,
    RetentionImpactField,
    RetentionPolicy,
    RetentionPolicyMode,
    RetentionPolicyState,
    category_for,
    check_retention_selection,
    empty_aggregate,
    exposure_seconds,
    newly_exposed_window,
    resolve_retention_policy,
)
from nvsop_contracts import ResolvedRetentionPolicy

# 该字段的影响事实归推理机本地 SQLite 所有；没有已接通的只读 Edge 统计通道时明确不可估算，
# 不能按零影响放行（#205 只补齐只读估算通道，不实现 #206 删除资格或 #207 回收）。
EDGE_LOCAL_USAGE_UNAVAILABLE = "edge_local_usage_channel_unavailable"
INSTANCE_CATEGORY_UNKNOWN = "instance_retention_category_unknown"
EVIDENCE_BYTES_UNKNOWN = "evidence_media_bytes_unknown"
EVIDENCE_REVIEW_UNKNOWN = "evidence_review_state_unknown"


@dataclass(frozen=True, slots=True)
class EdgeImpactResult:
    """所有请求来源均成功才返回计数;否则保留原因,不返回部分汇总冒充完整事实。"""

    breakdown: RetentionImpactBreakdown | None
    unavailable_reason: str | None
    sources: tuple[EdgeImpactSource, ...]

    def __post_init__(self) -> None:
        if (self.breakdown is None) == (self.unavailable_reason is None):
            raise ValueError("edge impact must carry counts or an unavailable reason")


class EdgeImpactGateway(Protocol):
    """retention 读取 Edge 本地已上报记录的只读估算 seam。"""

    def reported_impact(
        self,
        *,
        station_id: UUID | None,
        excluded_stations: frozenset[UUID],
        baseline_seconds: int | None,
        proposed_seconds: int,
        reference_time: datetime,
    ) -> EdgeImpactResult: ...


class EdgeImpactSourcesGateway(Protocol):
    """device 提供来源范围与最新签发配置,不提供或修改保留策略。"""

    def sources(self, *, station_id: UUID | None) -> tuple[EdgeImpactSource, ...]: ...


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

    def list_all(self) -> tuple[StationRetentionRecord, ...]:
        """返回全部覆盖记录；全局变更只影响没有自定义整组的工位，需要据此排除。"""
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


@dataclass(frozen=True, slots=True)
class EvidenceInstanceUsage:
    """evidence 拥有的某实例证据引用在窗口内的计数；按有无锁存违规引用拆开。

    ``linked`` 是带 ``violation_id``（指向一条已锁存违规）的引用，按 §5.19 恒受引用保护；
    ``unlinked`` 是没有锁存违规的引用。两者都是窗口内的真实计数，字节以登记大小为事实。
    """

    host_id: UUID
    station_id: UUID
    instance_id: int
    unlinked: ImpactAggregate
    linked: ImpactAggregate


class EvidenceImpactGateway(Protocol):
    """retention 读取 evidence owner 证据引用计数所用的最小只读 seam。"""

    def evidence_usage(
        self,
        *,
        station_id: UUID | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[EvidenceInstanceUsage, ...]:
        """按实例返回接收时刻落在 ``(exposed_from, exposed_to]`` 的证据引用计数。"""
        ...


@dataclass(frozen=True, slots=True)
class InstanceRetentionFact:
    """monitor 拥有的实例保留分类事实：真实判定、锁存违规与真实结案。

    ``verdict`` 为 ``None`` 表示中心没有该实例的判定镜像（未知，不得当作通过）；
    ``closed`` 只认真实 ``closed_at``，未结案或未知实例引用继续受保护。
    """

    host_id: UUID
    station_id: UUID
    instance_id: int
    verdict: str | None
    has_latched_violation: bool
    closed: bool
    # #206 尚无持久复核状态时返回 None，不能以结案或复核结果推断无人正在复核。
    active_review: bool | None = None


@dataclass(frozen=True, slots=True)
class StationUsage:
    """monitor 拥有的镜像事实按工位聚合的计数；受保护者单列，便于全局范围排除自定义工位。

    ``station_id`` 为 ``None`` 表示该批记录无法归属到单个工位（例如没有工位身份的健康事实）：
    它只计入全局范围，不随某工位范围返回，也不能被自定义工位排除。
    """

    station_id: UUID | None
    exposed: ImpactAggregate
    protected: ImpactAggregate


class MonitorImpactGateway(Protocol):
    """retention 读取 monitor owner 记录/处置影响计数与实例分类事实的最小只读 seam。"""

    def instance_facts(
        self, *, instance_ids: tuple[tuple[UUID, UUID, int], ...]
    ) -> tuple[InstanceRetentionFact, ...]:
        """返回这些实例的判定、锁存违规与真实结案事实；只查目标实例。"""
        ...

    def record_usage(
        self,
        *,
        station_id: UUID | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[StationUsage, ...]:
        """返回落在该时段的判定/健康/观测记录计数；未结案实例相关者单列为受保护。"""
        ...

    def disposal_usage(
        self,
        *,
        station_id: UUID | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[StationUsage, ...]:
        """返回落在该时段的处置记录计数；无明确结案事实的已锁存违规单列为受保护。"""
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
    _require_expected_revision(expected_revision)
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
    _require_expected_revision(expected_revision)
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


def estimate_global_policy_change(
    caller: Caller,
    policy: RetentionPolicy,
    *,
    expected_revision: int,
    policies: RetentionPolicyRepository,
    evidence_impact: EvidenceImpactGateway,
    monitor_impact: MonitorImpactGateway,
    edge_impact: EdgeImpactGateway,
    now: datetime,
    records: StationRetentionOverrideRepository,
) -> RetentionImpactEstimate:
    """估算整组替换全局策略会新暴露多少对象；只读，不写入也不删除任何对象。

    全局变更只影响整组跟随全局的工位：带自定义覆盖记录的工位不在本次全局影响范围内，逐字段
    计数时按工位排除。逐字段比较基准与目标，缺任一字段的当前事实即该字段不完整。
    ``records`` 必须提供覆盖事实，不能省略后猜测没有自定义工位。
    """
    authorize(caller, Permission.RETENTION_POLICY_EDIT)
    _require_expected_revision(expected_revision)
    state = policies.get_global()
    return _estimate_impact(
        baseline=DEFAULT_RETENTION_POLICY if state is None else state.policy,
        default_revision=0 if state is None else state.revision,
        selection_mode=None,
        baseline_revision=expected_revision,
        proposed=policy,
        station_id=None,
        excluded_stations=_custom_station_ids(records),
        evidence_impact=evidence_impact,
        monitor_impact=monitor_impact,
        edge_impact=edge_impact,
        now=now,
    )


def estimate_station_policy_change(
    caller: Caller,
    station_id: UUID,
    *,
    mode: RetentionPolicyMode,
    overrides: RetentionPolicy | None,
    expected_revision: int,
    policies: RetentionPolicyRepository,
    records: StationRetentionOverrideRepository,
    stations: StationFactsGateway,
    evidence_impact: EvidenceImpactGateway,
    monitor_impact: MonitorImpactGateway,
    edge_impact: EdgeImpactGateway,
    now: datetime,
) -> RetentionImpactEstimate:
    """估算整组替换工位覆盖会新暴露多少本工位对象；只读，不写入也不删除任何对象。"""
    authorize(caller, Permission.RETENTION_POLICY_EDIT)
    _require_expected_revision(expected_revision)
    try:
        check_retention_selection(mode=mode, overrides=overrides)
    except ValueError as error:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_INVALID) from error
    _require_active_station(stations, station_id)
    state = policies.get_global()
    baseline = _station_retention(station_id, policies, records.get(station_id)).effective
    proposed = resolve_retention_policy(
        default_retention_policy(policies), mode=mode, overrides=overrides
    )
    return _estimate_impact(
        baseline=baseline,
        default_revision=0 if state is None else state.revision,
        selection_mode=mode,
        baseline_revision=expected_revision,
        proposed=proposed,
        station_id=station_id,
        excluded_stations=frozenset(),
        evidence_impact=evidence_impact,
        monitor_impact=monitor_impact,
        edge_impact=edge_impact,
        now=now,
    )


def _estimate_impact(
    *,
    baseline: RetentionPolicy,
    default_revision: int,
    selection_mode: RetentionPolicyMode | None,
    baseline_revision: int,
    proposed: RetentionPolicy,
    station_id: UUID | None,
    excluded_stations: frozenset[UUID],
    evidence_impact: EvidenceImpactGateway,
    monitor_impact: MonitorImpactGateway,
    edge_impact: EdgeImpactGateway,
    now: datetime,
) -> RetentionImpactEstimate:
    """逐字段比较基准与目标阈值，只对“新增暴露的时段”向字段拥有者取真实计数。

    没有缩短的字段不构成新增暴露，直接跳过；无法取得事实的字段记为不可估算原因并使整次估算
    不完整。本函数不判断删除资格，也不删除任何对象。
    """
    evidence = _EvidenceFacts(
        impact=evidence_impact,
        monitor=monitor_impact,
        excluded_stations=excluded_stations,
    )
    monitor = _MonitorFacts(impact=monitor_impact, excluded_stations=excluded_stations)
    fields: list[RetentionFieldImpact] = []
    incomplete: list[str] = []
    for field, policy in FIELD_IMPACT.items():
        window = newly_exposed_window(baseline, proposed, field, now=now)
        if window is None:
            continue
        exposed_from, exposed_to = window
        breakdown: RetentionImpactBreakdown | None = None
        reason: str | None = None
        source_configurations: tuple[EdgeImpactSource, ...] = ()
        bytes_basis: str | None = None
        if policy.owner is RetentionFactOwner.EDGE:
            proposed_seconds = exposure_seconds(proposed, field)
            assert proposed_seconds is not None
            result = edge_impact.reported_impact(
                station_id=station_id,
                excluded_stations=excluded_stations,
                baseline_seconds=exposure_seconds(baseline, field),
                proposed_seconds=proposed_seconds,
                reference_time=now,
            )
            breakdown, reason = result.breakdown, result.unavailable_reason
            source_configurations = result.sources
            bytes_basis = "serialized_rows"
        elif policy.owner is RetentionFactOwner.EVIDENCE:
            assert policy.category is not None
            breakdown, reason = evidence.field_breakdown(
                category=policy.category,
                station_id=station_id,
                exposed_from=exposed_from,
                exposed_to=exposed_to,
            )
        elif policy.owner is RetentionFactOwner.MONITOR:
            breakdown, reason = monitor.field_breakdown(
                field=field,
                station_id=station_id,
                exposed_from=exposed_from,
                exposed_to=exposed_to,
            )
        else:
            assert_never(policy.owner)
        fields.append(
            RetentionFieldImpact(
                field=field,
                owner=policy.owner,
                category=policy.category,
                removes_objects=policy.removes_objects,
                exposed_from=exposed_from,
                exposed_to=exposed_to,
                breakdown=breakdown,
                unavailable_reason=reason,
                # 压缩/聚合不删除对象，但节省量不可预测：不冒充零、也不伪造精确空间。
                savings_unknown=not policy.removes_objects,
                bytes_basis=bytes_basis,
                source_configurations=source_configurations,
            )
        )
        if reason is not None:
            incomplete.append(f"{field.value}:{reason}")
    return RetentionImpactEstimate(
        baseline=baseline,
        default_revision=default_revision,
        selection_mode=selection_mode,
        excluded_stations=tuple(sorted(excluded_stations)),
        baseline_revision=baseline_revision,
        proposed=proposed,
        station_id=station_id,
        fields=tuple(fields),
        reference_time=now,
        incomplete_reasons=tuple(incomplete),
    )


class _EvidenceFacts:
    """按类别逐实例汇总证据引用；类别由 retention 的规则用 monitor 的实例事实解析。"""

    def __init__(
        self,
        *,
        impact: EvidenceImpactGateway,
        monitor: MonitorImpactGateway,
        excluded_stations: frozenset[UUID],
    ) -> None:
        self._impact = impact
        self._monitor = monitor
        self._excluded = excluded_stations
        self._usage: dict[
            tuple[UUID | None, datetime | None, datetime], tuple[EvidenceInstanceUsage, ...]
        ] = {}
        self._facts: dict[tuple[UUID, UUID, int], InstanceRetentionFact] = {}

    def field_breakdown(
        self,
        *,
        category: RetentionCategory,
        station_id: UUID | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[RetentionImpactBreakdown | None, str | None]:
        key = (station_id, exposed_from, exposed_to)
        if key not in self._usage:
            self._usage[key] = self._impact.evidence_usage(
                station_id=station_id, exposed_from=exposed_from, exposed_to=exposed_to
            )
        usages = tuple(
            usage for usage in self._usage[key] if usage.station_id not in self._excluded
        )
        self._load_facts(usages)
        exposed = empty_aggregate(measurable_bytes=True)
        protected = empty_aggregate(measurable_bytes=True)
        unknown_category = False
        unknown_bytes = False
        unknown_review = False
        for usage in usages:
            fact = self._facts.get((usage.host_id, usage.station_id, usage.instance_id))
            resolved = _resolve_category(fact)
            if fact is None or resolved is None:
                unknown_category = True
                continue
            if resolved is not category:
                continue
            # 两组互斥：已证明受保护者不同时计入会回收的对象。
            protected = protected.plus(usage.linked)
            if not fact.closed or fact.active_review is True:
                protected = protected.plus(usage.unlinked)
            elif usage.unlinked.objects and fact.active_review is None:
                unknown_review = True
            else:
                exposed = exposed.plus(usage.unlinked)
            if usage.unlinked.unknown_size_objects or usage.linked.unknown_size_objects:
                unknown_bytes = True
        if unknown_category:
            return None, INSTANCE_CATEGORY_UNKNOWN
        if unknown_bytes:
            return None, EVIDENCE_BYTES_UNKNOWN
        if unknown_review:
            return None, EVIDENCE_REVIEW_UNKNOWN
        return RetentionImpactBreakdown(exposed=exposed, protected=protected), None

    def _load_facts(self, usages: tuple[EvidenceInstanceUsage, ...]) -> None:
        """只为窗口内出现的实例取分类事实，不盘点整表。"""
        missing = tuple(
            sorted(
                {
                    (usage.host_id, usage.station_id, usage.instance_id)
                    for usage in usages
                    if (usage.host_id, usage.station_id, usage.instance_id) not in self._facts
                }
            )
        )
        if not missing:
            return
        for fact in self._monitor.instance_facts(instance_ids=missing):
            self._facts[(fact.host_id, fact.station_id, fact.instance_id)] = fact


class _MonitorFacts:
    """按工位汇总 monitor 记录/处置计数；排除自定义工位以尊重全局范围。"""

    def __init__(self, *, impact: MonitorImpactGateway, excluded_stations: frozenset[UUID]) -> None:
        self._impact = impact
        self._excluded = excluded_stations
        self._cache: dict[
            tuple[str, UUID | None, datetime | None, datetime], tuple[StationUsage, ...]
        ] = {}

    def field_breakdown(
        self,
        *,
        field: RetentionImpactField,
        station_id: UUID | None,
        exposed_from: datetime | None,
        exposed_to: datetime,
    ) -> tuple[RetentionImpactBreakdown | None, str | None]:
        kind = "disposal" if field is RetentionImpactField.DISPOSAL else "records"
        key = (kind, station_id, exposed_from, exposed_to)
        if key not in self._cache:
            query = self._impact.disposal_usage if kind == "disposal" else self._impact.record_usage
            self._cache[key] = query(
                station_id=station_id, exposed_from=exposed_from, exposed_to=exposed_to
            )
        exposed = empty_aggregate(measurable_bytes=True)
        protected = empty_aggregate(measurable_bytes=True)
        for usage in self._cache[key]:
            if usage.station_id in self._excluded:
                continue
            exposed = exposed.plus(usage.exposed)
            protected = protected.plus(usage.protected)
        return RetentionImpactBreakdown(exposed=exposed, protected=protected), None


def _resolve_category(fact: InstanceRetentionFact | None) -> RetentionCategory | None:
    """用 retention 自己的规则解析实例保留类别；事实不足时返回 ``None``（未知）。"""
    if fact is None:
        return None
    if fact.has_latched_violation:
        return RetentionCategory.FAIL
    if fact.verdict is None:
        return None
    try:
        return category_for(fact.verdict, has_latched_violation=False)
    except ValueError:
        return None


def _custom_station_ids(records: StationRetentionOverrideRepository) -> frozenset[UUID]:
    """全局变更只影响整组跟随全局的工位：带自定义整组的工位不在本次全局影响范围内。"""
    return frozenset(
        record.station_id
        for record in records.list_all()
        if record.mode is RetentionPolicyMode.CUSTOM
    )


def _require_expected_revision(expected_revision: int) -> None:
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")


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
    "EDGE_LOCAL_USAGE_UNAVAILABLE",
    "EVIDENCE_BYTES_UNKNOWN",
    "EVIDENCE_REVIEW_UNKNOWN",
    "INSTANCE_CATEGORY_UNKNOWN",
    "EdgeImpactGateway",
    "EdgeImpactResult",
    "EdgeImpactSourcesGateway",
    "EvidenceImpactGateway",
    "EvidenceInstanceUsage",
    "InstanceRetentionFact",
    "MonitorImpactGateway",
    "RetentionPolicyConflictError",
    "RetentionPolicyRepository",
    "RetentionStationPresence",
    "StationFactsGateway",
    "StationRetentionConfiguration",
    "StationRetentionOverrideRepository",
    "StationRetentionProjection",
    "StationRetentionProjectionGateway",
    "StationRetentionRecord",
    "StationUsage",
    "default_retention_policy",
    "estimate_global_policy_change",
    "estimate_station_policy_change",
    "get_policy",
    "read_station_retention",
    "update_policy",
    "update_station_retention",
]
