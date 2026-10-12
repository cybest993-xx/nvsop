"""再切片委托命令的专用主机签名端点（S038）。

**默认不挂载**：真实派发在 S039 能结案失败前保持关闭，生产 ``create_app`` 不包含本 router，
生产 Edge 的默认 ``/device-commands/next`` 只领取连接测试，不领取再切片。测试在显式装配的
应用接缝上验证完整的签名领取/成功回报链路，不伪造成功。

命令载荷由 ``evidence`` 拥有的请求与原始证据事实派生（工位/实例/锚点/窗口），领取走
``device`` 队列的同一幂等键与租约；成功回报先认证并结案命令, 再结案请求, 二者同一请求事务。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

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
from factory_sop.evidence.reclip import EvidenceReclipRefusedError, settle_reclip_success
from factory_sop.evidence.repository import EvidenceReclipRequestRepository, EvidenceRepository
from nvsop_contracts import (
    ReclipEvidenceClaim,
    ReclipEvidenceCommand,
    reclip_evidence_claim_to_wire,
)

router = APIRouter(prefix="/device-commands", tags=["evidence"])


class ReclipEvidenceResultBody(BaseModel):
    """再切片成功回报体：只声明中心已追加的新引用身份。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_id: str


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
    reclip = requests.find(claimed.request_id)
    original = None if reclip is None else evidence.find(reclip.evidence_id)
    if reclip is None or original is None:
        # 请求/原始证据缺失时不构造伪造命令, 也不静默丢弃领取; 留给后续执行票结案。
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    command = ReclipEvidenceCommand(
        command_id=str(claimed.command_id),
        request_id=str(reclip.request_id),
        station_id=original.registration.station_id,
        instance_id=original.registration.instance_id,
        anchor=reclip.anchor,
        window_start=reclip.window_start,
        window_end=reclip.window_end,
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
    body: ReclipEvidenceResultBody,
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
    """先认证并结案领取命令, 再结案请求的成功状态；任何拒绝都回滚整次写入。"""
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=body.model_dump(mode="json"),
        timestamp=host_timestamp,
        nonce=host_nonce,
        signature=host_signature,
    )
    gateway.complete_reclip_command(
        command_id=command_id,
        host=identity,
        claim_token=claim_token,
        now=datetime.now(UTC),
    )
    try:
        settled = settle_reclip_success(
            command_id=command_id,
            evidence_id=body.evidence_id,
            host_id=host_id,
            evidence=evidence,
            requests=requests,
        )
    except EvidenceReclipRefusedError as error:
        # 抛出让请求事务回滚命令结案, 不留下半结案的成功事实。
        raise HTTPException(status_code=error.status, detail=error.code) from error
    return {
        "accepted": True,
        "request_id": str(settled.request_id),
        "status": settled.status.value,
        "evidence_id": settled.result_evidence_id,
    }


__all__ = ["router"]
