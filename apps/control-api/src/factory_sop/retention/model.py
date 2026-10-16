"""retention 拥有的保留策略值与纯规则；默认值只在这里定义一次（§5.19）。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import cast
from uuid import UUID

from nvsop_contracts import EvidenceDeletionDecision


class RetentionCategory(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class RetentionMode(StrEnum):
    UNSET = "unset"
    DURATION = "duration"
    PERMANENT = "permanent"
    DISCARD = "discard"


class RetentionPolicyMode(StrEnum):
    """工位保留组的整组来源；与运行参数同一覆盖模式（§5.19）。"""

    FOLLOW_GLOBAL = "follow_global"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class RetentionRule:
    """单条保留规则：有界时长、永久或显式不保留。"""

    mode: RetentionMode
    seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RetentionMode):
            raise ValueError("retention rule mode is unsupported")
        if self.mode is RetentionMode.DURATION:
            if (
                isinstance(self.seconds, bool)
                or not isinstance(self.seconds, int)
                or self.seconds <= 0
            ):
                raise ValueError("duration retention requires a positive number of seconds")
        elif self.seconds is not None:
            raise ValueError("only duration retention carries seconds")

    def to_wire(self) -> dict[str, object]:
        return {"mode": self.mode.value, "seconds": self.seconds}

    @classmethod
    def from_wire(cls, value: object) -> RetentionRule:
        if not isinstance(value, Mapping) or set(value) != {"mode", "seconds"}:
            raise ValueError("retention rule must carry exactly mode and seconds")
        mode, seconds = value["mode"], value["seconds"]
        if not isinstance(mode, str):
            raise ValueError("retention rule mode must be a string")
        try:
            parsed = RetentionMode(mode)
        except ValueError:
            raise ValueError("retention rule mode is unsupported") from None
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, int)):
            raise ValueError("retention rule seconds must be an integer or null")
        return cls(parsed, seconds)


POLICY_FIELDS = (
    "evidence_pass",
    "evidence_fail",
    "evidence_indeterminate",
    "record_detail",
    "record_compression_age",
    "record_aggregate_age",
    "disposal",
    "local_reported",
)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """完整一组保留参数；工位覆盖（#204）将整组替换（§5.19）。

    录像滚动窗口与原始素材窗口不在这里——它们分别归 `device` 与边缘媒体配置。
    """

    evidence_pass: RetentionRule
    evidence_fail: RetentionRule
    evidence_indeterminate: RetentionRule
    record_detail: RetentionRule
    record_compression_age: RetentionRule
    record_aggregate_age: RetentionRule
    disposal: RetentionRule
    local_reported: RetentionRule

    def __post_init__(self) -> None:
        for name in POLICY_FIELDS:
            if getattr(self, name).mode is RetentionMode.UNSET:
                raise ValueError(f"{name} must be set; a complete policy has no unset field")
        for name in ("evidence_fail", "evidence_indeterminate", "disposal", "local_reported"):
            if getattr(self, name).mode is RetentionMode.DISCARD:
                raise ValueError(f"{name} must not be discard; discard is only for pass")
        for name in ("record_detail", "record_compression_age", "record_aggregate_age"):
            if getattr(self, name).mode is not RetentionMode.DURATION:
                raise ValueError(f"{name} must be a bounded duration")
        detail, compression = self.record_detail.seconds, self.record_compression_age.seconds
        if detail is None or compression is None:
            raise ValueError("record retention fields must be bounded durations")
        if detail < compression:
            raise ValueError("record detail retention must not be shorter than compression age")

    def to_wire(self) -> dict[str, object]:
        return {name: getattr(self, name).to_wire() for name in POLICY_FIELDS}

    @classmethod
    def from_wire(cls, value: object) -> RetentionPolicy:
        if not isinstance(value, Mapping) or set(value) != set(POLICY_FIELDS):
            raise ValueError("retention policy must be a complete group")
        return cls(**{name: RetentionRule.from_wire(value[name]) for name in POLICY_FIELDS})


@dataclass(frozen=True, slots=True)
class RetentionPolicyState:
    policy: RetentionPolicy
    revision: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError("retention policy revision must be a non-negative integer")


def check_retention_selection(
    *, mode: RetentionPolicyMode, overrides: RetentionPolicy | None
) -> None:
    """校验覆盖模式与覆盖组配对；不产生生效值。"""
    if mode is RetentionPolicyMode.FOLLOW_GLOBAL:
        if overrides is not None:
            raise ValueError("follow-global stations must not carry a custom retention group")
    elif mode is RetentionPolicyMode.CUSTOM:
        if overrides is None:
            raise ValueError("custom retention requires a complete override group")
    else:
        raise ValueError("retention policy mode is unsupported")


def resolve_retention_policy(
    default: RetentionPolicy,
    *,
    mode: RetentionPolicyMode,
    overrides: RetentionPolicy | None,
) -> RetentionPolicy:
    """解析一组生效保留值；跟随全局整组用默认，自定义整组替换，不逐字段合并。"""
    check_retention_selection(mode=mode, overrides=overrides)
    if mode is RetentionPolicyMode.FOLLOW_GLOBAL:
        return default
    assert overrides is not None
    return overrides


def category_for(verdict: str, *, has_latched_violation: bool) -> RetentionCategory:
    """解析实例保留类别；锁存违规恒按不通过类保留。`has_latched_violation` 由调用方传入。"""
    if has_latched_violation:
        return RetentionCategory.FAIL
    try:
        return RetentionCategory(verdict)
    except ValueError:
        raise ValueError(f"unsupported judgment verdict: {verdict}") from None


def check_reslice_window(expected_seconds: int, *, recording_window_seconds: int) -> None:
    """再切片期望不能超过录像滚动窗口；窗口值由拥有者传入（§5.20）。"""
    _positive_int(expected_seconds, "expected reslice window")
    _positive_int(recording_window_seconds, "recording window")
    if expected_seconds > recording_window_seconds:
        raise ValueError("expected reslice window must not exceed the recording window")


def _positive_int(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


_DAY = 24 * 60 * 60

DEFAULT_RETENTION_POLICY = RetentionPolicy(
    evidence_pass=RetentionRule(RetentionMode.DURATION, 90 * _DAY),
    evidence_fail=RetentionRule(RetentionMode.PERMANENT),
    evidence_indeterminate=RetentionRule(RetentionMode.DURATION, 90 * _DAY),
    record_detail=RetentionRule(RetentionMode.DURATION, 90 * _DAY),
    record_compression_age=RetentionRule(RetentionMode.DURATION, 7 * _DAY),
    record_aggregate_age=RetentionRule(RetentionMode.DURATION, 90 * _DAY),
    disposal=RetentionRule(RetentionMode.PERMANENT),
    local_reported=RetentionRule(RetentionMode.DURATION, 14 * _DAY),
)


class RetentionImpactField(StrEnum):
    """一次保留变更影响估算逐字段覆盖的保留字段；八个字段各有一个拥有者口径。"""

    EVIDENCE_PASS = "evidence_pass"
    EVIDENCE_FAIL = "evidence_fail"
    EVIDENCE_INDETERMINATE = "evidence_indeterminate"
    RECORD_DETAIL = "record_detail"
    RECORD_COMPRESSION_AGE = "record_compression_age"
    RECORD_AGGREGATE_AGE = "record_aggregate_age"
    DISPOSAL = "disposal"
    LOCAL_REPORTED = "local_reported"


class RetentionFactOwner(StrEnum):
    """字段影响事实的拥有者；缺任一拥有者的当前事实即估算不完整。"""

    EVIDENCE = "evidence"
    MONITOR = "monitor"
    EDGE = "edge"


@dataclass(frozen=True, slots=True)
class FieldImpactPolicy:
    """单个保留字段的影响口径：由哪个拥有者计数、对应哪个保留类别、是否删除对象。"""

    owner: RetentionFactOwner
    category: RetentionCategory | None
    removes_objects: bool


FIELD_IMPACT: dict[RetentionImpactField, FieldImpactPolicy] = {
    RetentionImpactField.EVIDENCE_PASS: FieldImpactPolicy(
        RetentionFactOwner.EVIDENCE, RetentionCategory.PASS, True
    ),
    RetentionImpactField.EVIDENCE_FAIL: FieldImpactPolicy(
        RetentionFactOwner.EVIDENCE, RetentionCategory.FAIL, True
    ),
    RetentionImpactField.EVIDENCE_INDETERMINATE: FieldImpactPolicy(
        RetentionFactOwner.EVIDENCE, RetentionCategory.INDETERMINATE, True
    ),
    RetentionImpactField.RECORD_DETAIL: FieldImpactPolicy(RetentionFactOwner.MONITOR, None, True),
    # 压缩/聚合不删除对象，但会让一批记录进入压缩/聚合范围：按真实对象/字节/时刻给出影响。
    # 节省量不可预测，因此既不冒充零、也不伪造精确物理空间。
    RetentionImpactField.RECORD_COMPRESSION_AGE: FieldImpactPolicy(
        RetentionFactOwner.MONITOR, None, False
    ),
    RetentionImpactField.RECORD_AGGREGATE_AGE: FieldImpactPolicy(
        RetentionFactOwner.MONITOR, None, False
    ),
    RetentionImpactField.DISPOSAL: FieldImpactPolicy(RetentionFactOwner.MONITOR, None, True),
    # 推理机本地已上报数据归边缘执行；没有已接通的只读 Edge 统计通道时该字段明确不可估算。
    RetentionImpactField.LOCAL_REPORTED: FieldImpactPolicy(RetentionFactOwner.EDGE, None, True),
}


def exposure_seconds(policy: RetentionPolicy, field: RetentionImpactField) -> int | None:
    """字段在给定策略下的暴露阈值秒数；永久（或未设置）永不暴露，返回 ``None``。"""
    rule = cast(RetentionRule, getattr(policy, field.value))
    if rule.mode is RetentionMode.DURATION:
        return rule.seconds
    if rule.mode is RetentionMode.DISCARD:
        return 0
    return None


def newly_exposed_window(
    baseline: RetentionPolicy,
    proposed: RetentionPolicy,
    field: RetentionImpactField,
    *,
    now: datetime,
) -> tuple[datetime | None, datetime] | None:
    """返回该字段缩短后新进入暴露范围的时段 ``(from, to]``；未缩短时返回 ``None``。

    逐字段比较基准与目标阈值：目标为永久则永不新增暴露；基准为永久则下界为空；两者都是有限时长
    时只在目标更短时新增一个有限区间。这样只缩短某一类不会因别的类更长而被整类跳过。
    """
    target = exposure_seconds(proposed, field)
    if target is None:
        return None
    baseline_seconds = exposure_seconds(baseline, field)
    if baseline_seconds is not None and target >= baseline_seconds:
        return None
    below = None if baseline_seconds is None else now - timedelta(seconds=baseline_seconds)
    return below, now - timedelta(seconds=target)


@dataclass(frozen=True, slots=True)
class ImpactAggregate:
    """某个时段内一组对象的真实计数；字节只在拥有者可度量时给出。"""

    objects: int
    known_bytes: int | None
    unknown_size_objects: int
    earliest: datetime | None
    latest: datetime | None

    def __post_init__(self) -> None:
        for name in ("objects", "unknown_size_objects"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"impact {name} must be a non-negative integer")
        if self.known_bytes is not None and (
            isinstance(self.known_bytes, bool)
            or not isinstance(self.known_bytes, int)
            or self.known_bytes < 0
        ):
            raise ValueError("impact known_bytes must be a non-negative integer or null")
        if self.unknown_size_objects > self.objects:
            raise ValueError("impact unknown-size objects must not exceed the object count")
        if self.earliest is not None and self.latest is not None and self.latest < self.earliest:
            raise ValueError("impact latest must not precede earliest")

    def plus(self, other: ImpactAggregate) -> ImpactAggregate:
        """合并两个时段的计数；任一侧字节不可度量时合并结果字节也不可度量。"""
        if self.known_bytes is None or other.known_bytes is None:
            known_bytes: int | None = None
        else:
            known_bytes = self.known_bytes + other.known_bytes
        return ImpactAggregate(
            objects=self.objects + other.objects,
            known_bytes=known_bytes,
            unknown_size_objects=self.unknown_size_objects + other.unknown_size_objects,
            earliest=_min_moment(self.earliest, other.earliest),
            latest=_max_moment(self.latest, other.latest),
        )


def empty_aggregate(*, measurable_bytes: bool) -> ImpactAggregate:
    """空计数；``measurable_bytes`` 说明该拥有者能否度量字节。"""
    return ImpactAggregate(
        objects=0,
        known_bytes=0 if measurable_bytes else None,
        unknown_size_objects=0,
        earliest=None,
        latest=None,
    )


def _min_moment(left: datetime | None, right: datetime | None) -> datetime | None:
    moments = [value for value in (left, right) if value is not None]
    return min(moments) if moments else None


def _max_moment(left: datetime | None, right: datetime | None) -> datetime | None:
    moments = [value for value in (left, right) if value is not None]
    return max(moments) if moments else None


@dataclass(frozen=True, slots=True)
class RetentionImpactBreakdown:
    """同一时段内会进入回收范围的对象，与被引用保护而跳过的对象，分开计数。"""

    exposed: ImpactAggregate
    protected: ImpactAggregate


@dataclass(frozen=True, slots=True)
class EdgeImpactSource:
    """device 提供的来源机最新已签发配置事实;缺失保持未知。"""

    host_id: UUID
    configuration_revision: int | None
    configuration_sha256: str | None


@dataclass(frozen=True, slots=True)
class RetentionFieldImpact:
    """单个保留字段的影响：有真实计数，或明确不可估算的原因；两者恰有其一。"""

    field: RetentionImpactField
    owner: RetentionFactOwner
    category: RetentionCategory | None
    removes_objects: bool
    exposed_from: datetime | None
    exposed_to: datetime
    breakdown: RetentionImpactBreakdown | None
    unavailable_reason: str | None
    savings_unknown: bool = False
    bytes_basis: str | None = None
    source_configurations: tuple[EdgeImpactSource, ...] = ()

    def __post_init__(self) -> None:
        if (self.breakdown is None) == (self.unavailable_reason is None):
            raise ValueError(
                "field impact must carry exactly one of counts or an unavailable reason"
            )


@dataclass(frozen=True, slots=True)
class RetentionImpactEstimate:
    """一次保留变更的逐字段影响估算；``complete`` 为假时不得用于确认。"""

    baseline: RetentionPolicy
    default_revision: int
    selection_mode: RetentionPolicyMode | None
    excluded_stations: tuple[UUID, ...]
    baseline_revision: int
    proposed: RetentionPolicy
    station_id: UUID | None
    fields: tuple[RetentionFieldImpact, ...]
    reference_time: datetime
    incomplete_reasons: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.incomplete_reasons


def impact_digest(estimate: RetentionImpactEstimate) -> str:
    """估算摘要的稳定摘要：绑定基准版本、目标策略、范围、参考时刻与真实计数。

    确认时必须原样带回本摘要；基准、目标、范围、真实计数或不完整原因任一变化都会改变摘要，
    服务端重算后不一致即拒绝，使旧确认不能用于盲签。
    """

    def aggregate(value: ImpactAggregate) -> dict[str, object]:
        return {
            "objects": value.objects,
            "known_bytes": value.known_bytes,
            "unknown_size_objects": value.unknown_size_objects,
            "earliest": None if value.earliest is None else value.earliest.isoformat(),
            "latest": None if value.latest is None else value.latest.isoformat(),
        }

    def field_entry(value: RetentionFieldImpact) -> dict[str, object]:
        return {
            "field": value.field.value,
            "owner": value.owner.value,
            "category": None if value.category is None else value.category.value,
            "removes_objects": value.removes_objects,
            "exposed_from": None if value.exposed_from is None else value.exposed_from.isoformat(),
            "exposed_to": value.exposed_to.isoformat(),
            "exposed": None if value.breakdown is None else aggregate(value.breakdown.exposed),
            "protected": None if value.breakdown is None else aggregate(value.breakdown.protected),
            "unavailable_reason": value.unavailable_reason,
            "savings_unknown": value.savings_unknown,
            "bytes_basis": value.bytes_basis,
            "source_configurations": [
                {
                    "host_id": str(source.host_id),
                    "configuration_revision": source.configuration_revision,
                    "configuration_sha256": source.configuration_sha256,
                }
                for source in value.source_configurations
            ],
        }

    canonical = json.dumps(
        {
            "revision": estimate.baseline_revision,
            "baseline": estimate.baseline.to_wire(),
            "default_revision": estimate.default_revision,
            "selection_mode": estimate.selection_mode,
            "excluded_stations": [str(value) for value in estimate.excluded_stations],
            "policy": estimate.proposed.to_wire(),
            "station_id": None if estimate.station_id is None else str(estimate.station_id),
            "reference_time": estimate.reference_time.isoformat(),
            "incomplete_reasons": list(estimate.incomplete_reasons),
            "fields": [field_entry(value) for value in estimate.fields],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode()).hexdigest()


class EvidenceMediaState(StrEnum):
    """evidence owner 报告的媒体状态；``unknown`` 表示登记事实不可解读，绝不能当作可用。"""

    AVAILABLE = "available"
    PENDING = "pending"
    FAILED = "failed"
    DELETED = "deleted"
    UNKNOWN = "unknown"


class EvidenceDeletionReason(StrEnum):
    """删除资格的机器原因码；``eligible``/``protected``/``unknown`` 三种判定都带一个解释。"""

    EXPIRED = "expired"
    RETENTION_DISCARD = "retention_discard"
    RETENTION_PERMANENT = "retention_permanent"
    NOT_EXPIRED = "not_expired"
    INSTANCE_OPEN = "instance_open"
    LATCHED_VIOLATION = "latched_violation"
    REVIEW_IN_PROGRESS = "review_in_progress"
    RECLIP_PENDING = "reclip_pending"
    PLAYBACK_IN_PROGRESS = "playback_in_progress"
    DELETION_CLAIM_PENDING = "deletion_claim_pending"
    EVIDENCE_UNREGISTERED = "evidence_unregistered"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    EVIDENCE_MEDIA_DELETED = "evidence_media_deleted"
    SOURCE_HOST_MISMATCH = "source_host_mismatch"
    GENERATION_MISMATCH = "generation_mismatch"
    MATERIAL_MISMATCH = "material_mismatch"
    REVIEW_STATE_UNKNOWN = "review_state_unknown"
    STATION_UNKNOWN = "station_unknown"
    STATION_DEACTIVATED = "station_deactivated"
    INSTANCE_UNKNOWN = "instance_unknown"
    INSTANCE_CATEGORY_UNKNOWN = "instance_category_unknown"
    RETENTION_POLICY_UNKNOWN = "retention_policy_unknown"
    AGE_UNKNOWN = "age_unknown"
    # 校验与反馈阶段的机器原因码：来源机按这些码记录跳过原因，不重试删除。
    TICKET_INVALID = "ticket_invalid"
    TICKET_EXPIRED = "ticket_expired"
    TICKET_TTL_UNCONFIGURED = "ticket_ttl_unconfigured"
    CONFIG_NOT_CURRENT = "config_not_current"
    CONFIG_CHANGED = "config_changed"
    POLICY_CHANGED = "policy_changed"
    SOURCE_HOST_UNREGISTERED = "source_host_unregistered"
    CLAIM_UNKNOWN = "claim_unknown"
    CLAIM_HOST_MISMATCH = "claim_host_mismatch"
    CLAIM_IDENTITY_MISMATCH = "claim_identity_mismatch"
    CLAIM_SETTLED = "claim_settled"
    CLAIM_ALREADY_SETTLED = "claim_already_settled"


@dataclass(frozen=True, slots=True)
class EvidenceDeletionFacts:
    """一条引用的删除资格事实；任一项未知都必须保持不删（§5.19 引用保护）。"""

    registered: bool
    media_state: EvidenceMediaState
    source_matches: bool
    generation_matches: bool
    material_matches: bool
    review_state_known: bool
    review_active: bool
    reclip_pending: bool
    playback_active: bool
    claim_pending: bool
    station_known: bool
    station_active: bool
    instance_known: bool
    instance_closed: bool
    latched_violation: bool
    category: RetentionCategory | None
    age_seconds: float | None


_EVIDENCE_CATEGORY_FIELD: dict[RetentionCategory, str] = {
    RetentionCategory.PASS: "evidence_pass",
    RetentionCategory.FAIL: "evidence_fail",
    RetentionCategory.INDETERMINATE: "evidence_indeterminate",
}


def evidence_retention_rule(
    policy: RetentionPolicy, category: RetentionCategory | None
) -> RetentionRule | None:
    """该类别在生效策略下的证据保留规则；类别未知时返回 ``None``（未知即不删）。"""
    if category is None:
        return None
    return cast(RetentionRule, getattr(policy, _EVIDENCE_CATEGORY_FIELD[category]))


def evidence_deletion_decision(
    facts: EvidenceDeletionFacts, *, rule: RetentionRule | None
) -> tuple[EvidenceDeletionDecision, EvidenceDeletionReason]:
    """纯删除资格规则：不可用、未知与仍成立的保护一律不删，只有确实不再保留且已过保留期才可删。

    判定顺序是安全顺序：先证明引用身份与登记状态可用，再确认没有仍成立的保护，最后才比较保留
    期。缺少任一事实都返回 ``unknown``（不删），绝不按"没有引用"放行；锁存违规与未结案实例
    不因保留期到期而解除保护。
    """
    if not facts.registered:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.EVIDENCE_UNREGISTERED
    if facts.media_state is EvidenceMediaState.DELETED:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.EVIDENCE_MEDIA_DELETED
    if facts.media_state is not EvidenceMediaState.AVAILABLE:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.EVIDENCE_UNAVAILABLE
    if not facts.source_matches:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.SOURCE_HOST_MISMATCH
    if not facts.generation_matches:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.GENERATION_MISMATCH
    if not facts.material_matches:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.MATERIAL_MISMATCH
    if not facts.review_state_known:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.REVIEW_STATE_UNKNOWN
    if facts.claim_pending:
        return EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.DELETION_CLAIM_PENDING
    if facts.review_active:
        return EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.REVIEW_IN_PROGRESS
    if facts.reclip_pending:
        return EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.RECLIP_PENDING
    if facts.playback_active:
        return EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.PLAYBACK_IN_PROGRESS
    if not facts.station_known:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.STATION_UNKNOWN
    if not facts.station_active:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.STATION_DEACTIVATED
    if not facts.instance_known:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.INSTANCE_UNKNOWN
    if facts.latched_violation:
        # 锁存违规是不可撤销的事实：其证据恒受保护，不因处置成功或类别时长而解除（§5.19）。
        return EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.LATCHED_VIOLATION
    if not facts.instance_closed:
        return EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.INSTANCE_OPEN
    if facts.category is None:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.INSTANCE_CATEGORY_UNKNOWN
    if rule is None or rule.mode is RetentionMode.UNSET:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.RETENTION_POLICY_UNKNOWN
    if rule.mode is RetentionMode.PERMANENT:
        return EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.RETENTION_PERMANENT
    if rule.mode is RetentionMode.DISCARD:
        return EvidenceDeletionDecision.ELIGIBLE, EvidenceDeletionReason.RETENTION_DISCARD
    if facts.age_seconds is None:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.AGE_UNKNOWN
    seconds = rule.seconds
    if seconds is None:
        return EvidenceDeletionDecision.UNKNOWN, EvidenceDeletionReason.RETENTION_POLICY_UNKNOWN
    if facts.age_seconds < seconds:
        return EvidenceDeletionDecision.PROTECTED, EvidenceDeletionReason.NOT_EXPIRED
    return EvidenceDeletionDecision.ELIGIBLE, EvidenceDeletionReason.EXPIRED


__all__ = [
    "DEFAULT_RETENTION_POLICY",
    "FIELD_IMPACT",
    "EvidenceDeletionFacts",
    "EvidenceDeletionReason",
    "EvidenceMediaState",
    "ImpactAggregate",
    "RetentionCategory",
    "RetentionFactOwner",
    "RetentionFieldImpact",
    "RetentionImpactBreakdown",
    "RetentionImpactEstimate",
    "RetentionImpactField",
    "RetentionMode",
    "RetentionPolicy",
    "RetentionPolicyMode",
    "RetentionPolicyState",
    "RetentionRule",
    "category_for",
    "check_reslice_window",
    "check_retention_selection",
    "empty_aggregate",
    "evidence_deletion_decision",
    "evidence_retention_rule",
    "exposure_seconds",
    "impact_digest",
    "newly_exposed_window",
    "resolve_retention_policy",
]
