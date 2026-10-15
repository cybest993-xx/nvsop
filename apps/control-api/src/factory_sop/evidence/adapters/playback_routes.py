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
    DeviceEvidenceGateway,
    DeviceHostGateway,
    DeviceMonitorGateway,
    host_identity_from_headers,
)
from factory_sop.evidence.adapters import dependencies
from factory_sop.evidence.adapters.routes import EvidenceRegistrationBody
from factory_sop.evidence.model import (
    EvidenceKind,
    EvidenceStatus,
    ReclipRequestStatus,
    ReviewConclusion,
)
from factory_sop.evidence.playback import (
    EvidenceAccessRefusedError,
    check_access_context,
    evidence_for_instance,
    issue_access_context,
)
from factory_sop.evidence.reclip import (
    EvidenceReclipRefusedError,
    create_reclip_request,
    list_reclip_requests,
    reclip_availability,
)
from factory_sop.evidence.repository import (
    EvidenceReclipRequestRepository,
    EvidenceRepository,
    EvidenceReviewRepository,
)
from factory_sop.evidence.reviews import (
    EvidenceReviewRefusedError,
    begin_review,
    cancel_review,
    list_reviews,
    save_review,
)
from factory_sop.problem import ApiErrorCode, problem_response
from factory_sop.settings import Settings

router = APIRouter(prefix="/evidence", tags=["evidence"])
Repository = Annotated[EvidenceRepository, Depends(dependencies.evidence)]
ReviewRepository = Annotated[EvidenceReviewRepository, Depends(dependencies.reviews)]
ReclipRepository = Annotated[EvidenceReclipRequestRepository, Depends(dependencies.reclip_requests)]
ReclipGateway = Annotated[DeviceEvidenceGateway, Depends(dependencies.reclip_gateway)]
HostAuthenticator = Annotated[DeviceHostGateway, Depends(dependencies.host_gateway)]
HostRegistry = Annotated[DeviceMonitorGateway, Depends(dependencies.registered_hosts)]


class EvidenceListing(EvidenceRegistrationBody):
    status: EvidenceStatus
    # 可再切片截止时刻（来源机墙钟 epoch 秒）与当前是否仍可再切片；旧登记缺冻结元数据时为未知。
    reclip_deadline: float | None = None
    reclip_available: bool = False


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


class EvidenceReviewHoldBody(BaseModel):
    """开始一次在审复核：稳定幂等 ID 与证据代次；不接受其它扩展字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    review_id: UUID = Field(strict=False)
    generation: str


class EvidenceReviewCancelBody(BaseModel):
    """取消本人在审占用；只认自己创建的 review_id。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    review_id: UUID = Field(strict=False)


class EvidenceReviewHoldRecord(BaseModel):
    review_id: UUID
    evidence_id: str
    generation: str
    reviewer_id: UUID
    started_at: datetime
    ended_at: datetime | None = None


class EvidenceReclipBody(BaseModel):
    """一次再切片请求：稳定幂等 ID、证据代次与新前后余量；锚点/主机/窗口不接受覆写。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    request_id: UUID = Field(strict=False)
    generation: str
    before_seconds: float = Field(allow_inf_nan=False)
    after_seconds: float = Field(allow_inf_nan=False)


class EvidenceReclipRecord(BaseModel):
    request_id: UUID
    evidence_id: str
    generation: str
    host_id: UUID
    anchor: float
    before_seconds: float
    after_seconds: float
    window_start: float
    window_end: float
    requested_by: UUID
    requested_at: datetime
    command_id: UUID
    status: ReclipRequestStatus
    failure_reason: str | None = None


@router.get(
    "",
    operation_id="readEvidence",
    response_model=list[EvidenceListing],
    openapi_extra=needs(Permission.EVIDENCE_VIEW),
)
def read_evidence(
    caller: Authorized,
    evidence: Repository,
    gateway: ReclipGateway,
    station_id: UUID,
    instance_id: int,
) -> list[dict[str, object]]:
    rows = evidence_for_instance(
        caller, evidence=evidence, station_id=str(station_id), instance_id=instance_id
    )
    now = datetime.now(UTC).timestamp()
    windows: dict[UUID, int | None] = {}
    result: list[dict[str, object]] = []
    for row in rows:
        host_id = UUID(row.registration.host_id)
        if host_id not in windows:
            windows[host_id] = gateway.recording_window_seconds(host_id=host_id)
        deadline, available = reclip_availability(
            row.registration,
            status=row.status,
            recording_window_seconds=windows[host_id],
            now=now,
        )
        result.append(
            asdict(row.registration)
            | {
                "status": row.status,
                "reclip_deadline": deadline,
                "reclip_available": available,
            }
        )
    return result


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
        return _review_refusal(error)
    return asdict(record)


@router.post(
    "/{evidence_id:path}/review-holds",
    operation_id="beginEvidenceReview",
    response_model=EvidenceReviewHoldRecord,
    openapi_extra=needs(Permission.EVIDENCE_VIEW, Permission.EVIDENCE_REVIEW_EDIT),
)
def begin_review_hold(
    evidence_id: str,
    body: EvidenceReviewHoldBody,
    caller: Authorized,
    evidence: Repository,
    reviews: ReviewRepository,
) -> dict[str, object] | Response:
    """进入在审保护：登记本人在该证据上的持久复核占用，删除认领在此之后被拒。"""
    try:
        hold = begin_review(
            caller,
            review_id=body.review_id,
            evidence_id=evidence_id,
            generation=body.generation,
            now=datetime.now(UTC),
            evidence=evidence,
            reviews=reviews,
        )
    except EvidenceReviewRefusedError as error:
        return _review_refusal(error)
    return asdict(hold)


@router.post(
    "/{evidence_id:path}/review-holds/cancel",
    operation_id="cancelEvidenceReview",
    status_code=204,
    openapi_extra=needs(Permission.EVIDENCE_VIEW, Permission.EVIDENCE_REVIEW_EDIT),
)
def cancel_review_hold(
    evidence_id: str,
    body: EvidenceReviewCancelBody,
    caller: Authorized,
    reviews: ReviewRepository,
) -> Response:
    """退出在审保护：只结束当前人自己的占用，其他人或已结案的占用不受影响。"""
    del evidence_id
    try:
        cancel_review(caller, review_id=body.review_id, now=datetime.now(UTC), reviews=reviews)
    except EvidenceReviewRefusedError as error:
        return _review_refusal(error)
    return Response(status_code=204)


def _review_refusal(error: EvidenceReviewRefusedError) -> Response:
    """把复核接缝的稳定原因码映射成问题响应；未知码保持 409 而不是静默成功。"""
    error_code, title = {
        "evidence_unregistered": (ApiErrorCode.OBJECT_NOT_FOUND, "证据未登记"),
        "evidence_generation_mismatch": (ApiErrorCode.RESOURCE_MISMATCH, "证据代次不匹配"),
        "evidence_unavailable": (ApiErrorCode.STATE_CONFLICT, "证据当前不可用，无法复核"),
        "evidence_deletion_claimed": (
            ApiErrorCode.STATE_CONFLICT,
            "证据删除认领未结案，暂不可复核",
        ),
        "review_state_unknown": (ApiErrorCode.STATE_CONFLICT, "历史复核状态未知，暂不可开启复核"),
        "review_conflict": (ApiErrorCode.IDEMPOTENCY_CONFLICT, "复核请求与既有结论冲突"),
        "review_not_owned_or_closed": (
            ApiErrorCode.STATE_CONFLICT,
            "复核占用不存在、已结束或不属于当前用户",
        ),
    }.get(error.code, (ApiErrorCode.STATE_CONFLICT, "复核请求被拒绝"))
    return problem_response(status=error.status, title=title, error_code=error_code)


@router.post(
    "/{evidence_id:path}/reclip-requests",
    operation_id="createEvidenceReclipRequest",
    response_model=EvidenceReclipRecord,
    openapi_extra=needs(Permission.EVIDENCE_VIEW, Permission.EVIDENCE_REVIEW_EDIT),
)
def request_reclip(
    evidence_id: str,
    body: EvidenceReclipBody,
    caller: Authorized,
    evidence: Repository,
    requests: ReclipRepository,
    gateway: ReclipGateway,
) -> dict[str, object] | Response:
    """登记一次再切片请求，并在同一事务内委托给来源主机的专用再切片命令队列。"""
    try:
        record = create_reclip_request(
            caller,
            request_id=body.request_id,
            evidence_id=evidence_id,
            generation=body.generation,
            before_seconds=body.before_seconds,
            after_seconds=body.after_seconds,
            now=datetime.now(UTC),
            evidence=evidence,
            requests=requests,
            gateway=gateway,
        )
    except EvidenceReclipRefusedError as error:
        error_code, title = {
            "evidence_unregistered": (ApiErrorCode.OBJECT_NOT_FOUND, "证据未登记"),
            "evidence_generation_mismatch": (ApiErrorCode.RESOURCE_MISMATCH, "证据代次不匹配"),
            "evidence_unavailable": (ApiErrorCode.STATE_CONFLICT, "证据当前不可用，无法再切片"),
            "reclip_metadata_unavailable": (
                ApiErrorCode.RESOURCE_MISMATCH,
                "缺少来源机冻结的时间信息，不可再切片",
            ),
            "reclip_source_host_unavailable": (
                ApiErrorCode.RESOURCE_MISMATCH,
                "来源推理机当前不可用",
            ),
            "reclip_window_expired": (ApiErrorCode.STATE_CONFLICT, "录像已过可再切片截止时刻"),
            "reclip_window_too_long": (ApiErrorCode.REQUEST_INVALID, "请求窗口超过录像滚动窗口"),
            "reclip_margins_invalid": (ApiErrorCode.REQUEST_INVALID, "再切片余量非法"),
            "reclip_request_conflict": (
                ApiErrorCode.IDEMPOTENCY_CONFLICT,
                "再切片请求与既有请求冲突",
            ),
        }[error.code]
        return problem_response(status=error.status, title=title, error_code=error_code)
    return asdict(record)


@router.get(
    "/{evidence_id:path}/reclip-requests",
    operation_id="readEvidenceReclipRequests",
    response_model=list[EvidenceReclipRecord],
    openapi_extra=needs(Permission.EVIDENCE_VIEW),
)
def read_reclip_requests(
    evidence_id: str, caller: Authorized, requests: ReclipRepository
) -> list[dict[str, object]]:
    """列出该证据的全部再切片请求；等待/失败状态可查，不隐含编辑。"""
    return [
        asdict(row)
        for row in list_reclip_requests(caller, evidence_id=evidence_id, requests=requests)
    ]


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
