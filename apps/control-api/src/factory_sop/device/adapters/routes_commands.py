"""委托连接测试命令的中心 HTTP 适配器。

操作员入口使用普通会话权限; 推理机入口使用注册的主机身份头, 因为推理机没有浏览器
会话。两个入口最终都穿过 `device` 用例, 因而领取、租约、配置修订和幂等规则只有一份。
领取令牌只在推理机领取响应中出现, 不进入操作员状态响应。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, ConfigDict, model_validator

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.adapters import route_support
from factory_sop.device.adapters.dependencies import connectors, hosts, pending_commands
from factory_sop.device.errors import DeviceRefusalCode
from factory_sop.device.model import (
    ConnectorReachability,
    ConnectorTestResult,
    DeviceStatus,
    InferenceHostIdentity,
    PendingCommand,
    PendingCommandStatus,
    PendingCommandType,
)
from factory_sop.device.repository import (
    ConnectorRepository,
    InferenceHostRepository,
    PendingCommandRepository,
)
from factory_sop.device.usecases.commands import (
    claim_next_command,
    complete_connection_test,
    enqueue_connector_connection_test,
    pending_command_by_identifier,
)
from factory_sop.problem import problem_openapi_response
from nvsop_contracts import (
    ConnectionTestClaim,
    ConnectionTestCommand,
    ConnectionTestOutcome,
    connection_test_claim_from_wire,
    connection_test_claim_to_wire,
    connection_test_command_from_wire,
    connection_test_result_from_wire,
)

router = APIRouter(prefix="/device-commands", tags=["device"])
connector_router = APIRouter(prefix="/connectors", tags=["device"])
ProblemResponses = dict[int | str, dict[str, Any]]

_OPERATOR_RESPONSES: ProblemResponses = (
    route_support._UNAUTHORIZED
    | route_support._NOT_FOUND_RESPONSES
    | route_support._CONFLICT_RESPONSES
)
_EDGE_RESPONSES: ProblemResponses = {
    401: problem_openapi_response("Inference host authentication failed"),
    403: problem_openapi_response("Inference host is not allowed to access this command"),
    404: route_support._NOT_FOUND_RESPONSES[404],
    409: route_support._CONFLICT_RESPONSES[409],
}

# 领取租约只覆盖一次有限的真实设备请求; 过期后由 PostgreSQL 适配器回收为 pending。
COMMAND_LEASE_DURATION = timedelta(minutes=5)
INFERENCE_HOST_ID_HEADER = "X-Inference-Host-ID"
INFERENCE_HOST_TOKEN_HEADER = "X-Inference-Host-Token"
COMMAND_CLAIM_TOKEN_HEADER = "X-Command-Claim-Token"


class PendingCommandView(BaseModel):
    """操作员可见的命令状态, 不含推理机领取令牌。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    host_id: UUID
    command_type: PendingCommandType
    target_id: UUID
    target_revision: int
    idempotency_key: str
    status: PendingCommandStatus
    attempt: int
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    result: ConnectorReachability | None
    result_detail: str | None
    failure_code: str | None
    completed_at: datetime | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


class ConnectionTestCommandDocument(BaseModel):
    """领取响应中的连接测试命令，与共享 contracts 的字段保持一致。"""

    model_config = ConfigDict(extra="forbid")

    command_type: Literal["test_connector_connection"]
    command_id: str
    connector_id: str
    connector_revision: int
    connector_type: str
    configuration: dict[str, str | int]

    @model_validator(mode="after")
    def _validate_shared_contract(self) -> Self:
        connection_test_command_from_wire(self.model_dump(mode="python"))
        return self


class ConnectionTestClaimDocument(BaseModel):
    """正式发布的领取响应契约，供 OpenAPI 和边缘运行时共同验证。"""

    model_config = ConfigDict(extra="forbid")

    command: ConnectionTestCommandDocument
    claim_token: str
    lease_expires_at: str

    @model_validator(mode="after")
    def _validate_shared_contract(self) -> Self:
        connection_test_claim_from_wire(self.model_dump(mode="python"))
        return self


class ConnectionTestResultDocument(BaseModel):
    """推理机回报的严格结果文档, 与共享 contracts 保持同一字段集。"""

    model_config = ConfigDict(extra="forbid")

    outcome: ConnectionTestOutcome
    detail: str | None
    credentials_configured: bool | None
    failure_code: str | None

    @model_validator(mode="after")
    def _validate_shared_contract(self) -> ConnectionTestResultDocument:
        """在请求验证阶段拒绝不完整的共享结果。"""
        connection_test_result_from_wire(self.model_dump(mode="python"))
        return self

    def to_domain(self) -> ConnectorTestResult:
        """将跨进程三态结果转换为中心持久化模型。"""
        contract = connection_test_result_from_wire(self.model_dump(mode="python"))
        match contract.outcome:
            case ConnectionTestOutcome.REACHABLE:
                return ConnectorTestResult(
                    reachability=ConnectorReachability.REACHABLE,
                    detail=contract.detail,
                    credentials_configured=contract.credentials_configured,
                )
            case ConnectionTestOutcome.UNREACHABLE:
                return ConnectorTestResult(
                    reachability=ConnectorReachability.UNREACHABLE,
                    detail=contract.detail,
                    credentials_configured=contract.credentials_configured,
                )
            case ConnectionTestOutcome.REJECTED:
                return ConnectorTestResult(
                    reachability=None,
                    detail=contract.detail,
                    credentials_configured=contract.credentials_configured,
                    failure_code=contract.failure_code or "COMMAND_RESULT_INVALID",
                )


def _view(command: PendingCommand, *, show_result: bool) -> PendingCommandView:
    """转换为状态视图；结果字段按调用方的查看权限裁剪。"""
    return PendingCommandView(
        id=command.id,
        host_id=command.host_id,
        command_type=command.command_type,
        target_id=command.target_id,
        target_revision=command.target_revision,
        idempotency_key=command.idempotency_key,
        status=command.status,
        attempt=command.attempt,
        claimed_at=command.claimed_at,
        lease_expires_at=command.lease_expires_at,
        result=command.result if show_result else None,
        result_detail=command.result_detail if show_result else None,
        failure_code=command.failure_code if show_result else None,
        completed_at=command.completed_at,
        created_by=command.created_by,
        created_at=command.created_at,
        updated_at=command.updated_at,
    )


def _lease_timestamp(value: datetime) -> str:
    """将中心时刻编码为共享契约要求的 UTC RFC3339。"""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@connector_router.post(
    "/{connector_id}/connection-test",
    status_code=status.HTTP_202_ACCEPTED,
    operation_id="enqueueConnectorConnectionTest",
    openapi_extra=needs(Permission.CONNECTOR_EDIT),
    responses=_OPERATOR_RESPONSES,
)
def enqueue_a_connector_connection_test(
    connector_id: UUID,
    caller: Authorized,
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    command_store: Annotated[PendingCommandRepository, Depends(pending_commands)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> PendingCommandView:
    """创建持久测试命令; 不同步访问设备, 也不接收凭据。"""
    command = enqueue_connector_connection_test(
        connector_id=connector_id,
        idempotency_key=idempotency_key,
        caller=caller,
        now=datetime.now(UTC),
        connectors=connector_store,
        hosts=host_store,
        commands=command_store,
    )
    return _view(command, show_result=caller.holds(Permission.CONNECTOR_VIEW))


@router.get(
    "/next",
    status_code=status.HTTP_200_OK,
    response_model=ConnectionTestClaimDocument,
    operation_id="claimNextDeviceCommand",
    responses=_EDGE_RESPONSES | {204: {"description": "No command is pending for this host"}},
)
def claim_next_device_command(
    host_id: Annotated[UUID, Header(alias=INFERENCE_HOST_ID_HEADER)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    command_store: Annotated[PendingCommandRepository, Depends(pending_commands)],
    host_token: Annotated[str | None, Header(alias=INFERENCE_HOST_TOKEN_HEADER)] = None,
) -> ConnectionTestClaimDocument | Response:
    """按认证主机领取一条命令; 无命令返回 204, 不会看到其他主机的队列。"""
    identity = InferenceHostIdentity(host_id=host_id, credential=host_token)
    now = datetime.now(UTC)
    command = claim_next_command(
        host=identity,
        now=now,
        lease_duration=COMMAND_LEASE_DURATION,
        hosts=host_store,
        commands=command_store,
    )
    if command is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if command.claim_token is None or command.lease_expires_at is None:
        raise RuntimeError("claimed command is missing its lease")

    connector = connector_store.by_id(command.target_id)
    if connector is None or connector.status is DeviceStatus.DEACTIVATED:
        # 目标在领取后消失或停用时立即结案, 避免队列永久卡住; 这不是设备不可达。
        detail = "委托命令目标不存在" if connector is None else "委托命令目标已停用"
        failure_code = (
            "COMMAND_TARGET_NOT_FOUND" if connector is None else "COMMAND_TARGET_DEACTIVATED"
        )
        complete_connection_test(
            command_id=command.id,
            host=identity,
            claim_token=command.claim_token,
            result=ConnectorTestResult(
                reachability=None,
                detail=detail,
                failure_code=failure_code,
            ),
            now=now,
            connectors=connector_store,
            hosts=host_store,
            commands=command_store,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if connector.revision != command.target_revision:
        # 领取前配置已变化时不下发新配置与旧修订号的混合对象。
        complete_connection_test(
            command_id=command.id,
            host=identity,
            claim_token=command.claim_token,
            result=ConnectorTestResult(
                reachability=None,
                detail="委托命令目标配置已变化",
                failure_code=DeviceRefusalCode.COMMAND_CONFIGURATION_CHANGED.value,
            ),
            now=now,
            connectors=connector_store,
            hosts=host_store,
            commands=command_store,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    delivery = ConnectionTestClaim(
        command=ConnectionTestCommand(
            command_id=str(command.id),
            connector_id=str(command.target_id),
            connector_revision=command.target_revision,
            connector_type=connector.connector_type.value,
            configuration=connector.configuration.to_wire(),
        ),
        claim_token=command.claim_token,
        lease_expires_at=_lease_timestamp(command.lease_expires_at),
    )
    return ConnectionTestClaimDocument.model_validate(connection_test_claim_to_wire(delivery))


@router.get(
    "/{command_id}",
    operation_id="readDeviceCommand",
    openapi_extra=needs(Permission.CONNECTOR_VIEW),
    responses=_OPERATOR_RESPONSES,
)
def read_a_device_command(
    command_id: UUID,
    caller: Authorized,
    command_store: Annotated[PendingCommandRepository, Depends(pending_commands)],
) -> PendingCommandView:
    """读取命令状态, 供 Web 轮询直到真实结果或明确拒绝。"""
    return _view(
        pending_command_by_identifier(
            command_id=command_id,
            caller=caller,
            commands=command_store,
        ),
        show_result=True,
    )


@router.post(
    "/{command_id}/result",
    operation_id="completeDeviceCommand",
    responses=_EDGE_RESPONSES,
)
def complete_a_device_command(
    command_id: UUID,
    result: ConnectionTestResultDocument,
    host_id: Annotated[UUID, Header(alias=INFERENCE_HOST_ID_HEADER)],
    claim_token: Annotated[
        str, Header(alias=COMMAND_CLAIM_TOKEN_HEADER, min_length=1, max_length=128)
    ],
    connector_store: Annotated[ConnectorRepository, Depends(connectors)],
    command_store: Annotated[PendingCommandRepository, Depends(pending_commands)],
    host_store: Annotated[InferenceHostRepository, Depends(hosts)],
    host_token: Annotated[str | None, Header(alias=INFERENCE_HOST_TOKEN_HEADER)] = None,
) -> PendingCommandView:
    """用主机身份、领取令牌和租约回报结果; 中心与连接器更新共用请求事务。"""
    completed = complete_connection_test(
        command_id=command_id,
        host=InferenceHostIdentity(host_id=host_id, credential=host_token),
        claim_token=claim_token,
        result=result.to_domain(),
        now=datetime.now(UTC),
        connectors=connector_store,
        hosts=host_store,
        commands=command_store,
    )
    return _view(completed, show_result=True)


__all__ = [
    "COMMAND_CLAIM_TOKEN_HEADER",
    "INFERENCE_HOST_ID_HEADER",
    "INFERENCE_HOST_TOKEN_HEADER",
    "router",
]
