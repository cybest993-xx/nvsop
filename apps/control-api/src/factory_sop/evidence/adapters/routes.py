"""中心接收来源推理机已落盘证据的签名元数据登记，不接收媒体字节。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, ValidationError

from factory_sop.device.api import DeviceHostGateway, host_identity_from_headers
from factory_sop.evidence.adapters import dependencies
from factory_sop.evidence.api import (
    EvidenceKind,
    EvidenceOrigin,
    EvidenceRefusedError,
    EvidenceRegistration,
    EvidenceRepository,
    EvidenceStatus,
    register_evidence,
)

router = APIRouter(prefix="/evidence", tags=["evidence"])


class EvidenceRegistrationBody(BaseModel):
    """只接受稳定身份、来源和本机受控引用，不接受媒体或任意扩展字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_id: str
    host_id: str
    station_id: str
    instance_id: int
    violation_id: str | None
    kind: Literal["clip", "keyframe"]
    origin: Literal["automatic", "reclip"]
    anchor: float
    window_start: float
    window_end: float
    generation: str
    sha256: str | None = None
    size: int | None = None
    reference: str | None = None


@router.post("/registrations", operation_id="registerEvidenceReference")
def report_evidence_reference(
    request: Request,
    body: dict[str, object],
    evidence: Annotated[EvidenceRepository, Depends(dependencies.evidence)],
    host_gateway: Annotated[DeviceHostGateway, Depends(dependencies.host_gateway)],
) -> dict[str, object]:
    try:
        payload = EvidenceRegistrationBody.model_validate(body)
        host_id = UUID(payload.host_id)
    except (ValidationError, ValueError) as error:
        raise HTTPException(status_code=422, detail="invalid evidence registration") from error
    if request.headers.get("X-Inference-Host-ID") != str(host_id):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="host identity mismatch"
        )
    identity = host_identity_from_headers(
        host_id=host_id,
        method=request.method,
        path=request.url.path,
        body=body,
        timestamp=request.headers.get("X-Inference-Host-Timestamp"),
        nonce=request.headers.get("X-Inference-Host-Nonce"),
        signature=request.headers.get("X-Inference-Host-Signature"),
    )
    host_gateway.authenticate(host=identity, now=datetime.now(UTC))

    registration_data = payload.model_dump()
    registration_data["kind"] = EvidenceKind(payload.kind)
    registration_data["origin"] = EvidenceOrigin(payload.origin)
    try:
        recorded = register_evidence(
            EvidenceRegistration(**registration_data),
            received_at=datetime.now(UTC),
            evidence=evidence,
            host_gateway=host_gateway,
        )
    except EvidenceRefusedError as error:
        raise HTTPException(status_code=409, detail=error.code.value) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid evidence registration") from error
    if recorded.status is not EvidenceStatus.AVAILABLE:
        raise HTTPException(status_code=422, detail="evidence material not available")
    return {"accepted": True, "evidence_id": recorded.evidence_id, "status": recorded.status.value}


__all__ = ["router"]
