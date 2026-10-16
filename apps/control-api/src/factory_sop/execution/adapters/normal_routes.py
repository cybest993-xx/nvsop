"""正常交权请求与旧机停机命令的 HTTP 适配器。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from factory_sop.auth.api import Authorized, Permission, needs
from factory_sop.device.api import (
    COMMAND_CLAIM_TOKEN_HEADER,
    COMMAND_LEASE_DURATION,
    INFERENCE_HOST_ID_HEADER,
    INFERENCE_HOST_NONCE_HEADER,
    INFERENCE_HOST_SIGNATURE_HEADER,
    INFERENCE_HOST_TIMESTAMP_HEADER,
    DeviceExecutionHandoverCommandGateway,
    host_identity_from_headers,
)
from factory_sop.execution.adapters import dependencies
from factory_sop.execution.errors import ExecutionRefusalCode, ExecutionRefusedError
from factory_sop.execution.model import NormalHandover, NormalHandoverStatus
from factory_sop.execution.repository import (
    ExecutionGrantRepository,
    NormalHandoverRepository,
)
from factory_sop.execution.usecases import (
    cancel_normal_handover,
    create_normal_handover,
    read_claimed_normal_handover,
    read_normal_handover,
    settle_normal_handover_command,
)
from factory_sop.problem import problem_openapi_response
from nvsop_contracts import (
    ExecutionHandoverClaim,
    ExecutionHandoverCommand,
    execution_handover_claim_to_wire,
    execution_handover_result_from_wire,
)

router = APIRouter(prefix="/execution", tags=["execution"])
command_router = APIRouter(prefix="/device-commands", tags=["execution"])
ProblemResponses = dict[int | str, dict[str, Any]]
_UNAUTHORIZED: ProblemResponses = {
    401: problem_openapi_response("Authentication required or session invalid"),
    403: problem_openapi_response("Permission denied or CSRF token invalid"),
}
_NOT_FOUND: ProblemResponses = {404: problem_openapi_response("Execution record not found")}
_CONFLICT: ProblemResponses = {409: problem_openapi_response("Execution state conflict")}
_UNPROCESSABLE: ProblemResponses = {422: problem_openapi_response("Request invalid")}
_EDGE_RESPONSES: ProblemResponses = {
    401: problem_openapi_response("Inference host authentication failed"),
    409: problem_openapi_response("Execution handover state conflict"),
}


class NormalHandoverCreation(BaseModel):
    """正常交权请求冻结的工位与双方主机身份。"""

    model_config = ConfigDict(extra="forbid")

    station_id: UUID
    from_host_id: UUID
    to_host_id: UUID


class NormalHandoverView(BaseModel):
    """正常交权状态及其冻结租约/命令身份。"""

    handover_id: UUID
    station_id: UUID
    from_host_id: UUID
    to_host_id: UUID
    grant_id: UUID
    command_id: UUID
    idempotency_key: str
    requested_by: UUID
    requested_at: datetime
    expires_at: datetime
    status: NormalHandoverStatus
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    timed_out_at: datetime | None
    failed_at: datetime | None
    failure_code: str | None
    failure_detail: str | None


def _view(record: NormalHandover) -> NormalHandoverView:
    return NormalHandoverView(
        handover_id=record.handover_id,
        station_id=record.station_id,
        from_host_id=record.from_host_id,
        to_host_id=record.to_host_id,
        grant_id=record.grant_id,
        command_id=record.command_id,
        idempotency_key=record.idempotency_key,
        requested_by=record.requested_by,
        requested_at=record.requested_at,
        expires_at=record.expires_at,
        status=record.status,
        confirmed_at=record.confirmed_at,
        cancelled_at=record.cancelled_at,
        timed_out_at=record.timed_out_at,
        failed_at=record.failed_at,
        failure_code=record.failure_code,
        failure_detail=record.failure_detail,
    )


def _lease_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@router.post(
    "/normal-handovers",
    status_code=status.HTTP_201_CREATED,
    operation_id="createNormalHandover",
    openapi_extra=needs(Permission.HANDOVER_EDIT),
    responses=_UNAUTHORIZED | _NOT_FOUND | _CONFLICT | _UNPROCESSABLE,
)
def create_normal(
    submission: NormalHandoverCreation,
    caller: Authorized,
    commands: Annotated[
        DeviceExecutionHandoverCommandGateway,
        Depends(dependencies.normal_handover_commands),
    ],
    handovers: Annotated[NormalHandoverRepository, Depends(dependencies.normal_handovers)],
    grants: Annotated[ExecutionGrantRepository, Depends(dependencies.grants)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> NormalHandoverView:
    """建立正常交权请求；中心收到请求不等于旧机已经停写。"""
    try:
        record = create_normal_handover(
            caller=caller,
            station_id=submission.station_id,
            from_host_id=submission.from_host_id,
            to_host_id=submission.to_host_id,
            idempotency_key=idempotency_key,
            now=datetime.now(UTC),
            handovers=handovers,
            grants=grants,
            commands=commands,
        )
    except IntegrityError as error:
        if getattr(error.orig, "sqlstate", None) != "23503":
            raise
        raise ExecutionRefusedError(ExecutionRefusalCode.HANDOVER_TARGET_NOT_FOUND) from error
    return _view(record)


@router.get(
    "/normal-handovers/{handover_id}",
    operation_id="readNormalHandover",
    openapi_extra=needs(Permission.HANDOVER_EDIT),
    responses=_UNAUTHORIZED | _NOT_FOUND,
)
def read_normal(
    handover_id: UUID,
    caller: Authorized,
    handovers: Annotated[NormalHandoverRepository, Depends(dependencies.normal_handovers)],
) -> NormalHandoverView:
    """读取并在需要时持久记录超时状态。"""
    return _view(
        read_normal_handover(
            caller=caller, handover_id=handover_id, now=datetime.now(UTC), handovers=handovers
        )
    )


@router.post(
    "/normal-handovers/{handover_id}/cancellation",
    operation_id="cancelNormalHandover",
    openapi_extra=needs(Permission.HANDOVER_EDIT),
    responses=_UNAUTHORIZED | _NOT_FOUND | _CONFLICT,
)
def cancel_normal(
    handover_id: UUID,
    caller: Authorized,
    handovers: Annotated[NormalHandoverRepository, Depends(dependencies.normal_handovers)],
    commands: Annotated[
        DeviceExecutionHandoverCommandGateway, Depends(dependencies.normal_handover_commands)
    ],
) -> NormalHandoverView:
    return _view(
        cancel_normal_handover(
            caller=caller,
            handover_id=handover_id,
            now=datetime.now(UTC),
            handovers=handovers,
            commands=commands,
        )
    )


@command_router.get(
    "/execution-handover/next",
    status_code=status.HTTP_200_OK,
    operation_id="claimNextExecutionHandoverCommand",
    response_model=None,
    responses=_EDGE_RESPONSES | {204: {"description": "No normal handover command is pending"}},
)
def claim_next_execution_handover(
    request: Request,
    host_id: Annotated[UUID, Header(alias=INFERENCE_HOST_ID_HEADER)],
    commands: Annotated[
        DeviceExecutionHandoverCommandGateway,
        Depends(dependencies.normal_handover_commands),
    ],
    handovers: Annotated[NormalHandoverRepository, Depends(dependencies.normal_handovers)],
    host_timestamp: Annotated[
        str | None, Header(alias=INFERENCE_HOST_TIMESTAMP_HEADER, min_length=1, max_length=32)
    ] = None,
    host_nonce: Annotated[
        str | None, Header(alias=INFERENCE_HOST_NONCE_HEADER, min_length=1, max_length=128)
    ] = None,
    host_signature: Annotated[
        str | None, Header(alias=INFERENCE_HOST_SIGNATURE_HEADER, min_length=1, max_length=4096)
    ] = None,
) -> dict[str, object] | Response:
    """只允许旧机按明确 command type 领取正常交权停机命令。"""
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=None,
        timestamp=host_timestamp,
        nonce=host_nonce,
        signature=host_signature,
    )
    now = datetime.now(UTC)
    claimed = commands.claim(host=identity, now=now, lease_duration=COMMAND_LEASE_DURATION)
    if claimed is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if claimed.claim_token is None or claimed.lease_expires_at is None:
        raise RuntimeError("claimed execution handover command is missing its lease")
    record = read_claimed_normal_handover(
        handover_id=claimed.target_id,
        command_id=claimed.id,
        from_host_id=host_id,
        now=now,
        handovers=handovers,
    )
    if record is None:
        commands.complete(
            command_id=claimed.id,
            host=identity,
            claim_token=claimed.claim_token,
            now=now,
            outcome="rejected",
            failure_code="NORMAL_HANDOVER_NOT_PENDING",
            detail="normal handover request is no longer pending",
            authenticated=True,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    command = ExecutionHandoverCommand(
        command_id=str(claimed.id),
        handover_id=str(record.handover_id),
        station_id=str(record.station_id),
        from_host_id=str(record.from_host_id),
        to_host_id=str(record.to_host_id),
        grant_id=str(record.grant_id),
    )
    return execution_handover_claim_to_wire(
        ExecutionHandoverClaim(
            command=command,
            claim_token=claimed.claim_token,
            lease_expires_at=_lease_timestamp(claimed.lease_expires_at),
        )
    )


@command_router.post(
    "/{command_id}/execution-handover-result",
    operation_id="completeExecutionHandoverCommand",
    responses=_EDGE_RESPONSES,
)
def complete_execution_handover(
    request: Request,
    command_id: UUID,
    body: dict[str, object],
    host_id: Annotated[UUID, Header(alias=INFERENCE_HOST_ID_HEADER)],
    claim_token: Annotated[
        str, Header(alias=COMMAND_CLAIM_TOKEN_HEADER, min_length=1, max_length=128)
    ],
    commands: Annotated[
        DeviceExecutionHandoverCommandGateway,
        Depends(dependencies.normal_handover_commands),
    ],
    handovers: Annotated[NormalHandoverRepository, Depends(dependencies.normal_handovers)],
    grants: Annotated[ExecutionGrantRepository, Depends(dependencies.grants)],
    host_timestamp: Annotated[
        str | None, Header(alias=INFERENCE_HOST_TIMESTAMP_HEADER, min_length=1, max_length=32)
    ] = None,
    host_nonce: Annotated[
        str | None, Header(alias=INFERENCE_HOST_NONCE_HEADER, min_length=1, max_length=128)
    ] = None,
    host_signature: Annotated[
        str | None, Header(alias=INFERENCE_HOST_SIGNATURE_HEADER, min_length=1, max_length=4096)
    ] = None,
) -> dict[str, object]:
    """认证旧机回报，在同一事务内更新请求与命令终态。"""
    try:
        result = execution_handover_result_from_wire(body)
        handover_id = UUID(result.handover_id)
        grant_id = UUID(result.grant_id)
    except ValueError as error:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("body",), "msg": "invalid execution handover result"}]
        ) from error
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=body,
        timestamp=host_timestamp,
        nonce=host_nonce,
        signature=host_signature,
    )
    record, accepted = settle_normal_handover_command(
        handover_id=handover_id,
        command_id=command_id,
        # 工位/双方主机身份以冻结请求为准: 客户端只证明 handover_id/grant_id 与自己的主机身份,
        # 不自报 station_id/to_host_id 冒充另一台推理机的交权。
        station_id=None,
        from_host_id=host_id,
        to_host_id=None,
        grant_id=grant_id,
        host=identity,
        claim_token=claim_token,
        outcome=result.outcome.value,
        failure_code=result.failure_code,
        detail=result.detail,
        now=datetime.now(UTC),
        handovers=handovers,
        grants=grants,
        commands=commands,
    )
    return {
        "accepted": accepted,
        "handover_id": result.handover_id,
        "status": None if record is None else record.status.value,
    }


__all__ = ["command_router", "router"]
