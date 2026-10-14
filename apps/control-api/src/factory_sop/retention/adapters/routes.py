"""retention 保留策略 HTTP 适配器：全局默认、工位整组覆盖与变更影响估算。

缩短保留组前必须先经 `POST .../impact-estimate` 取得影响估算与确认令牌；`PUT` 确认时原样带回
该令牌，服务端按令牌记录的参考时刻重算同一份估算并比对摘要。基准、目标、范围或真实计数任一变化
都会使旧令牌失效；任一字段缺少当前事实（未知类别、未结案、无法度量的字节、没有统计通道）都会使
整次估算不完整并拒绝确认，避免拿旧确认或不完整确认盲签。确认只改变后续回收依据，不同步删除对象。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.observability import get_logger
from factory_sop.problem import problem_openapi_response
from factory_sop.retention.adapters.dependencies import (
    edge_impact,
    estimate_ttl_seconds,
    evidence_impact,
    monitor_impact,
    policy_repository,
    station_facts,
    station_overrides,
)
from factory_sop.retention.adapters.estimate_token import (
    decode_estimate_token,
    encode_estimate_token,
)
from factory_sop.retention.api import (
    EdgeImpactGateway,
    EvidenceImpactGateway,
    MonitorImpactGateway,
    RetentionCategory,
    RetentionFactOwner,
    RetentionImpactEstimate,
    RetentionImpactField,
    RetentionMode,
    RetentionPolicy,
    RetentionPolicyConflictError,
    RetentionPolicyMode,
    RetentionPolicyRepository,
    RetentionRule,
    StationFactsGateway,
    StationRetentionOverrideRepository,
    StationRetentionRecord,
    default_retention_policy,
    estimate_global_policy_change,
    estimate_station_policy_change,
    get_policy,
    impact_digest,
    read_station_retention,
    update_policy,
    update_station_retention,
)
from factory_sop.retention.errors import RetentionRefusalCode, RetentionRefusedError
from factory_sop.retention.model import POLICY_FIELDS

ProblemResponses = dict[int | str, dict[str, Any]]

_logger = get_logger("retention")

_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}
_NOT_FOUND_RESPONSES: ProblemResponses = {
    404: problem_openapi_response("Station not found"),
}
_CONFIRM_RESPONSES: ProblemResponses = _NOT_FOUND_RESPONSES | {
    409: problem_openapi_response(
        "Retention policy conflict, stale estimate or incomplete estimate"
    ),
}
_ESTIMATE_RESPONSES: ProblemResponses = _NOT_FOUND_RESPONSES | {
    422: problem_openapi_response("Retention policy group is invalid"),
}

router = APIRouter(prefix="/retention", tags=["retention"], responses=_UNAUTHORIZED)


class RetentionRuleView(BaseModel):
    """一条保留规则的 wire 形状；生效组不出现 `unset`。"""

    model_config = {"from_attributes": True}

    mode: RetentionMode
    seconds: int | None = None


class RetentionPolicyView(BaseModel):
    """完整一组保留参数；缺字段或不合法的一律拒绝。"""

    model_config = {"from_attributes": True}

    evidence_pass: RetentionRuleView
    evidence_fail: RetentionRuleView
    evidence_indeterminate: RetentionRuleView
    record_detail: RetentionRuleView
    record_compression_age: RetentionRuleView
    record_aggregate_age: RetentionRuleView
    disposal: RetentionRuleView
    local_reported: RetentionRuleView

    def to_domain(self) -> RetentionPolicy:
        try:
            return RetentionPolicy(
                **{
                    name: RetentionRule(
                        mode=getattr(self, name).mode, seconds=getattr(self, name).seconds
                    )
                    for name in POLICY_FIELDS
                }
            )
        except ValueError as error:
            raise RetentionRefusedError(RetentionRefusalCode.POLICY_INVALID) from error


class GlobalRetentionPolicyView(BaseModel):
    policy: RetentionPolicyView
    revision: int


class GlobalRetentionPolicyUpdate(BaseModel):
    policy: RetentionPolicyView


class StationRetentionView(BaseModel):
    """工位保留的默认组、覆盖组与唯一生效组；不逐字段混合。"""

    model_config = {"from_attributes": True}

    station_id: UUID
    # 该工位覆盖记录自身的版本；0 表示尚无记录，整组跟随全局默认。
    revision: int
    mode: RetentionPolicyMode
    default: RetentionPolicyView
    overrides: RetentionPolicyView | None
    effective: RetentionPolicyView


class StationRetentionUpdate(BaseModel):
    mode: RetentionPolicyMode
    overrides: RetentionPolicyView | None = None


class ImpactAggregateView(BaseModel):
    """一个时段内一组对象的真实计数；`known_bytes` 为空表示该拥有者无法度量字节。"""

    model_config = {"from_attributes": True}

    objects: int
    known_bytes: int | None
    unknown_size_objects: int
    earliest: datetime | None
    latest: datetime | None


class EdgeImpactSourceView(BaseModel):
    model_config = {"from_attributes": True}

    host_id: UUID
    configuration_revision: int | None
    configuration_sha256: str | None


class RetentionFieldImpactView(BaseModel):
    """单个保留字段的影响：有真实计数，或明确不可估算的原因；两者恰有其一。"""

    field: RetentionImpactField
    owner: RetentionFactOwner
    category: RetentionCategory | None
    removes_objects: bool
    exposed_from: datetime | None
    exposed_to: datetime
    exposed: ImpactAggregateView | None
    protected: ImpactAggregateView | None
    unavailable_reason: str | None
    savings_unknown: bool
    bytes_basis: str | None
    source_configurations: list[EdgeImpactSourceView]


class RetentionImpactEstimateView(BaseModel):
    """一次保留变更的逐字段影响估算；`complete` 为假时不得确认。"""

    baseline_revision: int
    proposed: RetentionPolicyView
    station_id: UUID | None
    fields: list[RetentionFieldImpactView]
    reference_time: datetime
    incomplete_reasons: list[str]
    complete: bool
    # 令牌绑定本次估算；不完整估算的令牌在确认端会被重算拒绝，从不构成放行。
    estimate_token: str


def _global_view(policy: RetentionPolicy, revision: int) -> GlobalRetentionPolicyView:
    return GlobalRetentionPolicyView(
        policy=RetentionPolicyView.model_validate(policy), revision=revision
    )


def _estimate_view(estimate: RetentionImpactEstimate) -> RetentionImpactEstimateView:
    return RetentionImpactEstimateView(
        baseline_revision=estimate.baseline_revision,
        proposed=RetentionPolicyView.model_validate(estimate.proposed),
        station_id=estimate.station_id,
        fields=[
            RetentionFieldImpactView(
                field=value.field,
                owner=value.owner,
                category=value.category,
                removes_objects=value.removes_objects,
                exposed_from=value.exposed_from,
                exposed_to=value.exposed_to,
                exposed=(
                    None
                    if value.breakdown is None
                    else ImpactAggregateView.model_validate(value.breakdown.exposed)
                ),
                protected=(
                    None
                    if value.breakdown is None
                    else ImpactAggregateView.model_validate(value.breakdown.protected)
                ),
                unavailable_reason=value.unavailable_reason,
                savings_unknown=value.savings_unknown,
                bytes_basis=value.bytes_basis,
                source_configurations=[
                    EdgeImpactSourceView.model_validate(source)
                    for source in value.source_configurations
                ],
            )
            for value in estimate.fields
        ],
        incomplete_reasons=list(estimate.incomplete_reasons),
        reference_time=estimate.reference_time,
        complete=estimate.complete,
        # 令牌绑定本次估算（基准/目标/范围/真实计数/参考时刻）；不完整时确认端会重算并拒绝，
        # 令牌本身从不构成放行。
        estimate_token=encode_estimate_token(estimate),
    )


def _global_revision(policies: RetentionPolicyRepository) -> int:
    state = policies.get_global()
    return 0 if state is None else state.revision


def _station_revision(records: StationRetentionOverrideRepository, station_id: UUID) -> int:
    record = records.get(station_id)
    return 0 if record is None else record.revision


def _estimate_log(estimate: RetentionImpactEstimate) -> dict[str, object]:
    """诊断用的估算摘要：只含稳定摘要、逐字段对象数与不完整原因，不含策略秘密或媒体内容。"""
    return {
        "digest": impact_digest(estimate),
        "reference_time": estimate.reference_time.isoformat(),
        "complete": estimate.complete,
        "incomplete_reasons": list(estimate.incomplete_reasons),
        "fields": [
            {
                "field": value.field.value,
                "exposed": None if value.breakdown is None else value.breakdown.exposed.objects,
                "protected": (
                    None if value.breakdown is None else value.breakdown.protected.objects
                ),
            }
            for value in estimate.fields
        ],
    }


def _station_record_log(record: StationRetentionRecord | None) -> dict[str, object] | None:
    """工位覆盖记录的变更前值；没有记录即整组跟随全局默认。"""
    if record is None:
        return None
    return {
        "mode": record.mode.value,
        "overrides": None if record.overrides is None else record.overrides.to_wire(),
    }


def _require_confirmable(
    estimate: RetentionImpactEstimate, *, ttl_seconds: int | None, now: datetime
) -> None:
    """缩短必须有完整事实和已配置、未到期的确认；延长不依赖确认时限。"""
    if not estimate.complete:
        raise RetentionRefusedError(
            RetentionRefusalCode.POLICY_ESTIMATE_INCOMPLETE,
            detail="; ".join(estimate.incomplete_reasons),
        )
    if estimate.fields:
        if ttl_seconds is None:
            raise RetentionRefusedError(RetentionRefusalCode.POLICY_ESTIMATE_TTL_UNCONFIGURED)
        age = (now - estimate.reference_time).total_seconds()
        if age < 0 or age >= ttl_seconds:
            raise RetentionRefusedError(RetentionRefusalCode.POLICY_ESTIMATE_STALE)


@router.get(
    "/policy",
    operation_id="readRetentionPolicy",
    openapi_extra=needs(Permission.RETENTION_POLICY_VIEW),
)
def read_the_retention_policy(
    caller: Authorized,
    policies: Annotated[RetentionPolicyRepository, Depends(policy_repository)],
) -> GlobalRetentionPolicyView:
    """读取全局默认保留策略；尚未持久化时是代码默认值，不写库。"""
    state = get_policy(caller, policies)
    return _global_view(state.policy, state.revision)


@router.post(
    "/policy/impact-estimate",
    operation_id="estimateRetentionPolicyImpact",
    openapi_extra=needs(Permission.RETENTION_POLICY_EDIT),
    responses=_ESTIMATE_RESPONSES,
)
def estimate_retention_policy_impact(
    caller: Authorized,
    submitted: GlobalRetentionPolicyUpdate,
    policies: Annotated[RetentionPolicyRepository, Depends(policy_repository)],
    records: Annotated[StationRetentionOverrideRepository, Depends(station_overrides)],
    evidence: Annotated[EvidenceImpactGateway, Depends(evidence_impact)],
    monitor: Annotated[MonitorImpactGateway, Depends(monitor_impact)],
    edge: Annotated[EdgeImpactGateway, Depends(edge_impact)],
) -> RetentionImpactEstimateView:
    """逐字段按数据拥有者的真实事实估算新暴露的对象/空间/时刻；只读，不写策略。

    全局变更只影响整组跟随全局的工位：带自定义整组的工位不在本次影响范围内。
    """
    estimate = estimate_global_policy_change(
        caller,
        submitted.policy.to_domain(),
        expected_revision=_global_revision(policies),
        policies=policies,
        records=records,
        evidence_impact=evidence,
        monitor_impact=monitor,
        edge_impact=edge,
        now=datetime.now(UTC),
    )
    return _estimate_view(estimate)


@router.put(
    "/policy",
    operation_id="updateRetentionPolicy",
    openapi_extra=needs(Permission.RETENTION_POLICY_EDIT),
    responses=_CONFIRM_RESPONSES,
)
def update_the_retention_policy(
    caller: Authorized,
    submitted: GlobalRetentionPolicyUpdate,
    policies: Annotated[RetentionPolicyRepository, Depends(policy_repository)],
    records: Annotated[StationRetentionOverrideRepository, Depends(station_overrides)],
    evidence: Annotated[EvidenceImpactGateway, Depends(evidence_impact)],
    monitor: Annotated[MonitorImpactGateway, Depends(monitor_impact)],
    edge: Annotated[EdgeImpactGateway, Depends(edge_impact)],
    ttl_seconds: Annotated[int | None, Depends(estimate_ttl_seconds)],
    if_match: Annotated[int, Header(alias="If-Match", ge=0)],
    estimate_token: Annotated[str | None, Header(alias="X-Retention-Estimate")] = None,
) -> GlobalRetentionPolicyView:
    """整组替换全局默认策略；只改变后续回收依据，不同步删除任何数据。

    必须携带来自影响估算的确认令牌；不完整、基准/目标/计数变化都会使令牌无效或禁止确认。
    """
    policy = submitted.policy.to_domain()
    token = decode_estimate_token(estimate_token)
    now = datetime.now(UTC)
    # 先按 owner 接口重算估算：授权由用例负责，未授权调用在此即被拒绝。
    estimate = estimate_global_policy_change(
        caller,
        policy,
        expected_revision=if_match,
        policies=policies,
        records=records,
        evidence_impact=evidence,
        monitor_impact=monitor,
        edge_impact=edge,
        now=now if token is None else token.reference_time,
    )
    if token is None:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_ESTIMATE_REQUIRED)
    _require_confirmable(estimate, ttl_seconds=ttl_seconds, now=datetime.now(UTC))
    if impact_digest(estimate) != token.digest:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_ESTIMATE_STALE)
    before = default_retention_policy(policies)
    try:
        state = update_policy(caller, policy, expected_revision=if_match, repository=policies)
    except RetentionPolicyConflictError as error:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_CONFLICT) from error
    _logger.info(
        "retention.policy.changed",
        scope="global",
        operator_id=str(caller.user.id),
        operator=caller.user.display_name,
        before=before.to_wire(),
        after=policy.to_wire(),
        estimate=_estimate_log(estimate),
    )
    return _global_view(state.policy, state.revision)


@router.get(
    "/stations/{station_id}",
    operation_id="readStationRetention",
    openapi_extra=needs(Permission.RETENTION_POLICY_VIEW),
    responses=_NOT_FOUND_RESPONSES,
)
def read_station_retention_policy(
    station_id: UUID,
    caller: Authorized,
    policies: Annotated[RetentionPolicyRepository, Depends(policy_repository)],
    records: Annotated[StationRetentionOverrideRepository, Depends(station_overrides)],
    stations: Annotated[StationFactsGateway, Depends(station_facts)],
) -> StationRetentionView:
    """读取全局默认、工位覆盖与唯一生效组；只读，不写入。"""
    return StationRetentionView.model_validate(
        read_station_retention(
            caller, station_id, policies=policies, overrides=records, stations=stations
        )
    )


@router.post(
    "/stations/{station_id}/impact-estimate",
    operation_id="estimateStationRetentionImpact",
    openapi_extra=needs(Permission.RETENTION_POLICY_EDIT),
    responses=_ESTIMATE_RESPONSES,
)
def estimate_station_retention_impact(
    station_id: UUID,
    submitted: StationRetentionUpdate,
    caller: Authorized,
    policies: Annotated[RetentionPolicyRepository, Depends(policy_repository)],
    records: Annotated[StationRetentionOverrideRepository, Depends(station_overrides)],
    stations: Annotated[StationFactsGateway, Depends(station_facts)],
    evidence: Annotated[EvidenceImpactGateway, Depends(evidence_impact)],
    monitor: Annotated[MonitorImpactGateway, Depends(monitor_impact)],
    edge: Annotated[EdgeImpactGateway, Depends(edge_impact)],
) -> RetentionImpactEstimateView:
    """逐字段估算替换该工位覆盖后新暴露的本工位对象/空间/时刻；只读，不写记录。"""
    estimate = estimate_station_policy_change(
        caller,
        station_id,
        mode=submitted.mode,
        overrides=None if submitted.overrides is None else submitted.overrides.to_domain(),
        expected_revision=_station_revision(records, station_id),
        policies=policies,
        records=records,
        stations=stations,
        evidence_impact=evidence,
        monitor_impact=monitor,
        edge_impact=edge,
        now=datetime.now(UTC),
    )
    return _estimate_view(estimate)


@router.put(
    "/stations/{station_id}",
    operation_id="updateStationRetention",
    openapi_extra=needs(Permission.RETENTION_POLICY_EDIT),
    responses=_CONFIRM_RESPONSES,
)
def update_station_retention_policy(
    station_id: UUID,
    submitted: StationRetentionUpdate,
    caller: Authorized,
    policies: Annotated[RetentionPolicyRepository, Depends(policy_repository)],
    records: Annotated[StationRetentionOverrideRepository, Depends(station_overrides)],
    stations: Annotated[StationFactsGateway, Depends(station_facts)],
    evidence: Annotated[EvidenceImpactGateway, Depends(evidence_impact)],
    monitor: Annotated[MonitorImpactGateway, Depends(monitor_impact)],
    edge: Annotated[EdgeImpactGateway, Depends(edge_impact)],
    ttl_seconds: Annotated[int | None, Depends(estimate_ttl_seconds)],
    if_match: Annotated[int, Header(alias="If-Match", ge=0)],
    estimate_token: Annotated[str | None, Header(alias="X-Retention-Estimate")] = None,
) -> StationRetentionView:
    """整组替换工位保留覆盖；If-Match 是该工位覆盖记录版本，0 表示首次建立。

    必须先做本工位影响估算并带回确认令牌；不完整、基准/目标/计数变化都会使令牌无效或禁止确认。
    """
    overrides = None if submitted.overrides is None else submitted.overrides.to_domain()
    before = _station_record_log(records.get(station_id))
    token = decode_estimate_token(estimate_token)
    now = datetime.now(UTC)
    # 先按 owner 接口重算估算：授权由用例负责，未授权调用在此即被拒绝。
    estimate = estimate_station_policy_change(
        caller,
        station_id,
        mode=submitted.mode,
        overrides=overrides,
        expected_revision=if_match,
        policies=policies,
        records=records,
        stations=stations,
        evidence_impact=evidence,
        monitor_impact=monitor,
        edge_impact=edge,
        now=now if token is None else token.reference_time,
    )
    if token is None:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_ESTIMATE_REQUIRED)
    _require_confirmable(estimate, ttl_seconds=ttl_seconds, now=datetime.now(UTC))
    if impact_digest(estimate) != token.digest:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_ESTIMATE_STALE)
    try:
        configuration = update_station_retention(
            caller,
            station_id,
            mode=submitted.mode,
            overrides=overrides,
            expected_revision=if_match,
            policies=policies,
            records=records,
            stations=stations,
        )
    except RetentionPolicyConflictError as error:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_CONFLICT) from error
    _logger.info(
        "retention.policy.changed",
        scope="station",
        station_id=str(station_id),
        operator_id=str(caller.user.id),
        operator=caller.user.display_name,
        before=before,
        after={
            "mode": submitted.mode.value,
            "overrides": None if overrides is None else overrides.to_wire(),
        },
        estimate=_estimate_log(estimate),
    )
    return StationRetentionView.model_validate(configuration)


__all__ = ["router"]
