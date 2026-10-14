"""retention 保留策略 HTTP 适配器：全局默认与工位整组覆盖。"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.problem import problem_openapi_response
from factory_sop.retention.adapters.dependencies import (
    policy_repository,
    station_facts,
    station_overrides,
)
from factory_sop.retention.api import (
    RetentionMode,
    RetentionPolicy,
    RetentionPolicyConflictError,
    RetentionPolicyMode,
    RetentionPolicyRepository,
    RetentionRule,
    StationFactsGateway,
    StationRetentionOverrideRepository,
    get_policy,
    read_station_retention,
    update_policy,
    update_station_retention,
)
from factory_sop.retention.errors import RetentionRefusalCode, RetentionRefusedError
from factory_sop.retention.model import POLICY_FIELDS

ProblemResponses = dict[int | str, dict[str, Any]]

_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}
_NOT_FOUND_RESPONSES: ProblemResponses = {
    404: problem_openapi_response("Station not found"),
}
_ITEM_RESPONSES: ProblemResponses = _NOT_FOUND_RESPONSES | {
    409: problem_openapi_response("Retention policy conflict"),
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


def _global_view(policy: RetentionPolicy, revision: int) -> GlobalRetentionPolicyView:
    return GlobalRetentionPolicyView(
        policy=RetentionPolicyView.model_validate(policy), revision=revision
    )


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


@router.put(
    "/policy",
    operation_id="updateRetentionPolicy",
    openapi_extra=needs(Permission.RETENTION_POLICY_EDIT),
    responses={409: problem_openapi_response("Retention policy conflict")},
)
def update_the_retention_policy(
    caller: Authorized,
    submitted: GlobalRetentionPolicyUpdate,
    policies: Annotated[RetentionPolicyRepository, Depends(policy_repository)],
    if_match: Annotated[int, Header(alias="If-Match", ge=0)],
) -> GlobalRetentionPolicyView:
    """整组替换全局默认策略；只改变后续回收依据，不同步删除任何数据。"""
    policy = submitted.policy.to_domain()
    try:
        state = update_policy(caller, policy, expected_revision=if_match, repository=policies)
    except RetentionPolicyConflictError as error:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_CONFLICT) from error
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


@router.put(
    "/stations/{station_id}",
    operation_id="updateStationRetention",
    openapi_extra=needs(Permission.RETENTION_POLICY_EDIT),
    responses=_ITEM_RESPONSES,
)
def update_station_retention_policy(
    station_id: UUID,
    submitted: StationRetentionUpdate,
    caller: Authorized,
    policies: Annotated[RetentionPolicyRepository, Depends(policy_repository)],
    records: Annotated[StationRetentionOverrideRepository, Depends(station_overrides)],
    stations: Annotated[StationFactsGateway, Depends(station_facts)],
    if_match: Annotated[int, Header(alias="If-Match", ge=0)],
) -> StationRetentionView:
    """整组替换工位保留覆盖；If-Match 是该工位覆盖记录版本，0 表示首次建立。"""
    try:
        configuration = update_station_retention(
            caller,
            station_id,
            mode=submitted.mode,
            overrides=None if submitted.overrides is None else submitted.overrides.to_domain(),
            expected_revision=if_match,
            policies=policies,
            records=records,
            stations=stations,
        )
    except RetentionPolicyConflictError as error:
        raise RetentionRefusedError(RetentionRefusalCode.POLICY_CONFLICT) from error
    return StationRetentionView.model_validate(configuration)


__all__ = ["router"]
