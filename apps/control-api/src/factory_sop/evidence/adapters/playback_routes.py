"""浏览器签发与来源主机回查；只返回元数据，不代理媒体。"""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from factory_sop.auth.api import (
    Authorized,
    CurrentSessionFingerprint,
    Permission,
    SessionFingerprintRecheck,
    needs,
)
from factory_sop.device.api import (
    DeviceHostGateway,
    DeviceMonitorGateway,
    host_identity_from_headers,
)
from factory_sop.evidence.adapters import dependencies
from factory_sop.evidence.adapters.routes import EvidenceRegistrationBody
from factory_sop.evidence.model import EvidenceKind, EvidenceStatus, ReviewConclusion
from factory_sop.evidence.playback import (
    EvidenceAccessRefusedError,
    check_access_context,
    evidence_for_instance,
    issue_access_context,
)
from factory_sop.evidence.repository import EvidenceRepository, EvidenceReviewRepository
from factory_sop.evidence.reviews import (
    EvidenceReviewRefusedError,
    list_reviews,
    save_review,
)
from factory_sop.problem import ApiErrorCode, problem_response
from factory_sop.settings import Settings

router = APIRouter(prefix="/evidence", tags=["evidence"])
Repository = Annotated[EvidenceRepository, Depends(dependencies.evidence)]
ReviewRepository = Annotated[EvidenceReviewRepository, Depends(dependencies.reviews)]
HostAuthenticator = Annotated[DeviceHostGateway, Depends(dependencies.host_gateway)]
HostRegistry = Annotated[DeviceMonitorGateway, Depends(dependencies.registered_hosts)]


class EvidenceListing(EvidenceRegistrationBody):
    status: EvidenceStatus


class EvidenceAccessContext(BaseModel):
    context: str
    origin: str
    expires_at: int
    host_id: UUID


class EvidenceAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    evidence_id: str
    context: str


class EvidenceMedia(BaseModel):
    reference: str
    size: int
    sha256: str
    kind: EvidenceKind


class EvidenceReviewBody(BaseModel):
    """一次人工复核提交：稳定幂等 ID、证据代次与结论；不接受其它扩展字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    # JSON 解码后是字符串；strict 模式下 UUID 只接受实例，故仅此字段放宽容忍字符串。
    review_id: UUID = Field(strict=False)
    generation: str
    conclusion: Literal["confirmed", "rejected"]


class EvidenceReviewRecord(BaseModel):
    review_id: UUID
    evidence_id: str
    generation: str
    reviewer_id: UUID
    reviewer_name: str
    reviewed_at: datetime
    conclusion: ReviewConclusion


@router.get(
    "",
    operation_id="readEvidence",
    response_model=list[EvidenceListing],
    openapi_extra=needs(Permission.EVIDENCE_VIEW),
)
def read_evidence(
    caller: Authorized, evidence: Repository, station_id: UUID, instance_id: int
) -> list[dict[str, object]]:
    return [
        asdict(row.registration) | {"status": row.status}
        for row in evidence_for_instance(
            caller, evidence=evidence, station_id=str(station_id), instance_id=instance_id
        )
    ]


@router.get(
    "/{evidence_id:path}/access-context",
    operation_id="createEvidenceAccessContext",
    response_model=EvidenceAccessContext,
    openapi_extra=needs(Permission.EVIDENCE_VIEW),
)
def create_context(
    request: Request,
    response: Response,
    evidence_id: str,
    caller: Authorized,
    fingerprint: CurrentSessionFingerprint,
    evidence: Repository,
    registry: HostRegistry,
) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    response.headers["Cache-Control"] = "no-store"
    return issue_access_context(
        caller,
        evidence_id=evidence_id,
        fingerprint=fingerprint,
        evidence=evidence,
        registered_hosts=registry.registered_host_ids(),
        origins=settings.evidence_host_origins,
        secret=settings.csrf_secret.get_secret_value(),
        now=datetime.now(UTC).timestamp(),
    )


@router.get(
    "/{evidence_id:path}/reviews",
    operation_id="readEvidenceReviews",
    response_model=list[EvidenceReviewRecord],
    openapi_extra=needs(Permission.EVIDENCE_VIEW),
)
def read_reviews(
    evidence_id: str, caller: Authorized, reviews: ReviewRepository
) -> list[dict[str, object]]:
    return [asdict(row) for row in list_reviews(caller, evidence_id=evidence_id, reviews=reviews)]


@router.post(
    "/{evidence_id:path}/reviews",
    operation_id="registerEvidenceReview",
    response_model=EvidenceReviewRecord,
    openapi_extra=needs(Permission.EVIDENCE_VIEW, Permission.EVIDENCE_REVIEW_EDIT),
)
def register_review(
    evidence_id: str,
    body: EvidenceReviewBody,
    caller: Authorized,
    evidence: Repository,
    reviews: ReviewRepository,
) -> dict[str, object] | Response:
    """同步登记一条人工复核结论；只返回已持久化的记录，不走 jobs。"""
    try:
        record = save_review(
            caller,
            review_id=body.review_id,
            evidence_id=evidence_id,
            generation=body.generation,
            conclusion=ReviewConclusion(body.conclusion),
            reviewed_at=datetime.now(UTC),
            evidence=evidence,
            reviews=reviews,
        )
    except EvidenceReviewRefusedError as error:
        error_code, title = {
            "evidence_unregistered": (ApiErrorCode.OBJECT_NOT_FOUND, "证据未登记"),
            "evidence_generation_mismatch": (ApiErrorCode.RESOURCE_MISMATCH, "证据代次不匹配"),
            "evidence_unavailable": (ApiErrorCode.STATE_CONFLICT, "证据当前不可用，无法复核"),
            "review_conflict": (ApiErrorCode.IDEMPOTENCY_CONFLICT, "复核请求与既有结论冲突"),
        }[error.code]
        return problem_response(status=error.status, title=title, error_code=error_code)
    return asdict(record)


@router.post("/access-check", operation_id="checkEvidenceAccess", response_model=EvidenceMedia)
def check_context(
    request: Request,
    response: Response,
    body: EvidenceAccessRequest,
    evidence: Repository,
    gateway: HostAuthenticator,
    recheck: SessionFingerprintRecheck,
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    try:
        host_id = UUID(request.headers.get("X-Inference-Host-ID", ""))
    except ValueError as error:
        raise EvidenceAccessRefusedError(403, "host_identity_required") from error
    now = datetime.now(UTC)
    gateway.authenticate(
        host=host_identity_from_headers(
            host_id=host_id,
            method=request.method,
            path=request.url.path,
            body=body.model_dump(),
            timestamp=request.headers.get("X-Inference-Host-Timestamp"),
            nonce=request.headers.get("X-Inference-Host-Nonce"),
            signature=request.headers.get("X-Inference-Host-Signature"),
        ),
        now=now,
    )
    settings: Settings = request.app.state.settings
    return check_access_context(
        context=body.context,
        evidence_id=body.evidence_id,
        host_id=str(host_id),
        evidence=evidence,
        resolve_caller=recheck,
        secret=settings.csrf_secret.get_secret_value(),
        now=now.timestamp(),
    )
