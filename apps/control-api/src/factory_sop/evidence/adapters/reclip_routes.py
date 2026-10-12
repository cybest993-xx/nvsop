"""再切片委托命令的专用主机签名端点（S038/S039）。

生产 ``create_app`` 挂载本 router；默认 ``/device-commands/next`` 仍只领取连接测试，再切片命令
只走专用签名端点。命令载荷由 ``evidence`` 拥有的请求与原始证据事实派生（工位/实例/锚点/窗口），
领取走 ``device`` 队列的同一幂等键与租约；回报先认证并结案命令, 再在同一请求事务内结案请求的
成功或终局失败。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from factory_sop.device.api import (
    COMMAND_CLAIM_TOKEN_HEADER,
    COMMAND_LEASE_DURATION,
    INFERENCE_HOST_ID_HEADER,
    INFERENCE_HOST_NONCE_HEADER,
    INFERENCE_HOST_SIGNATURE_HEADER,
    INFERENCE_HOST_TIMESTAMP_HEADER,
    DeviceEvidenceGateway,
    host_identity_from_headers,
)
from factory_sop.evidence.adapters import dependencies
from factory_sop.evidence.model import ReclipRequestStatus
from factory_sop.evidence.reclip import (
    EvidenceReclipRefusedError,
    settle_reclip_failure,
    settle_reclip_success,
)
from factory_sop.evidence.repository import EvidenceReclipRequestRepository, EvidenceRepository
from nvsop_contracts import (
    ReclipEvidenceClaim,
    ReclipEvidenceCommand,
    reclip_evidence_claim_to_wire,
    reclip_evidence_result_from_wire,
)

router = APIRouter(prefix="/device-commands", tags=["evidence"])


ReclipGateway = Annotated[DeviceEvidenceGateway, Depends(dependencies.reclip_gateway)]
ReclipRepository = Annotated[EvidenceReclipRequestRepository, Depends(dependencies.reclip_requests)]
EvidenceStore = Annotated[EvidenceRepository, Depends(dependencies.evidence)]


def _lease_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@router.get(
    "/reclip/next",
    status_code=status.HTTP_200_OK,
    operation_id="claimNextReclipCommand",
    response_model=None,
)
def claim_next_reclip_command(
    request: Request,
    host_id: Annotated[UUID, Header(alias=INFERENCE_HOST_ID_HEADER)],
    gateway: ReclipGateway,
    requests: ReclipRepository,
    evidence: EvidenceStore,
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
    """按已验证主机身份领取一条再切片命令；无命令返回 204。"""
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=None,
        timestamp=host_timestamp,
        nonce=host_nonce,
        signature=host_signature,
    )
    claimed = gateway.claim_reclip_command(
        host=identity, now=datetime.now(UTC), lease_duration=COMMAND_LEASE_DURATION
    )
    if claimed is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    now = datetime.now(UTC)
    reclip = requests.find(claimed.request_id)
    original = None if reclip is None else evidence.find(reclip.evidence_id)
    if reclip is None or original is None:
        # 请求/原始证据在领取前消失: 命令必须终局结案, 不静默 204 留下永久领取循环。
        gateway.fail_claimed_reclip_command(
            claimed=claimed,
            host_id=host_id,
            failure_code="reclip_source_missing",
            detail="reclip request or original evidence no longer exists",
            now=now,
        )
        # 请求仍在时同一事务也结案为失败, 不留下永久 pending (AC1); 请求已消失则只有命令可留。
        if reclip is not None and reclip.status is ReclipRequestStatus.PENDING:
            settle_reclip_failure(
                command_id=claimed.command_id,
                failure_code="reclip_source_missing",
                detail="original evidence no longer exists",
                requests=requests,
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    registration = original.registration
    recording_window = gateway.recording_window_seconds(host_id=reclip.host_id)
    if registration.wall_offset is None or recording_window is None:
        gateway.fail_claimed_reclip_command(
            claimed=claimed,
            host_id=host_id,
            failure_code="reclip_source_host_unavailable",
            detail="source host recording window or frozen wall mapping is unavailable",
            now=now,
        )
        if reclip.status is ReclipRequestStatus.PENDING:
            settle_reclip_failure(
                command_id=claimed.command_id,
                failure_code="reclip_source_host_unavailable",
                detail="source host recording window or frozen wall mapping is unavailable",
                requests=requests,
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    # 权威截止由中心按原登记冻结墙钟映射与来源机滚动窗口算出; 来源机不得自行猜归档截止。
    available_until = reclip.window_start + registration.wall_offset + recording_window
    command = ReclipEvidenceCommand(
        command_id=str(claimed.command_id),
        request_id=str(reclip.request_id),
        station_id=registration.station_id,
        instance_id=registration.instance_id,
        anchor=reclip.anchor,
        window_start=reclip.window_start,
        window_end=reclip.window_end,
        available_until=available_until,
    )
    claim = ReclipEvidenceClaim(
        command=command,
        claim_token=claimed.claim_token,
        lease_expires_at=_lease_timestamp(claimed.lease_expires_at),
    )
    return reclip_evidence_claim_to_wire(claim)


@router.post(
    "/{command_id}/reclip-result",
    status_code=status.HTTP_200_OK,
    operation_id="completeReclipCommand",
    response_model=None,
)
def complete_reclip_command(
    request: Request,
    command_id: UUID,
    body: dict[str, object],
    host_id: Annotated[UUID, Header(alias=INFERENCE_HOST_ID_HEADER)],
    claim_token: Annotated[
        str, Header(alias=COMMAND_CLAIM_TOKEN_HEADER, min_length=1, max_length=128)
    ],
    gateway: ReclipGateway,
    requests: ReclipRepository,
    evidence: EvidenceStore,
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
    """先认证并结案领取命令, 再在同一请求事务内结案请求的成功或终局失败；拒绝都回滚整次写入。"""
    try:
        result = reclip_evidence_result_from_wire(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid reclip result") from error
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=body,
        timestamp=host_timestamp,
        nonce=host_nonce,
        signature=host_signature,
    )
    gateway.complete_reclip_command(
        command_id=command_id,
        host=identity,
        claim_token=claim_token,
        failure_code=result.failure_code,
        detail=result.detail,
        now=datetime.now(UTC),
    )
    try:
        if result.evidence_id is not None:
            settled = settle_reclip_success(
                command_id=command_id,
                evidence_id=result.evidence_id,
                host_id=host_id,
                evidence=evidence,
                requests=requests,
            )
        else:
            assert result.failure_code is not None
            settled = settle_reclip_failure(
                command_id=command_id,
                failure_code=result.failure_code,
                detail=result.detail,
                requests=requests,
            )
    except EvidenceReclipRefusedError as error:
        # 抛出让请求事务回滚命令结案, 不留下半结案事实。
        raise HTTPException(status_code=error.status, detail=error.code) from error
    if settled.status is ReclipRequestStatus.SUCCEEDED:
        return {
            "accepted": True,
            "request_id": str(settled.request_id),
            "status": settled.status.value,
            "evidence_id": settled.result_evidence_id,
        }
    return {
        "accepted": True,
        "request_id": str(settled.request_id),
        "status": settled.status.value,
        "failure_reason": settled.failure_reason,
    }


__all__ = ["router"]
