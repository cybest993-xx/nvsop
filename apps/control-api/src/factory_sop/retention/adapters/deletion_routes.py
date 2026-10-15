"""证据删除资格的三个主机签名动作：claim / verify / result（#206）。

来源机在删除本机媒体之前必须持有与中心当前事实一致的资格，因此这里的三个动作只服务已登记主机：

- ``POST /retention/evidence-deletion-claims``：提交来源机自己已确认的配置身份与一批本机引用，
  取回逐条 ``eligible/protected/unknown`` 判定、原因码与短期票据；
- ``POST .../verify``：删除前用票据重新校验同一条引用；
- ``POST .../result``：把一条认领终局反馈为 ``deleted/retained/cancelled``。

三个入口都不接受浏览器会话或人类权限，只使用登记公钥、时间戳与 nonce 认证（与其它机器端点同一
边界）。未知字段、越界批次与非法身份在解析阶段就被拒绝；批次级前置条件（未配置票据有效期、来源
配置不是中心当前配置、来源主机未登记）整批拒绝，并带上稳定的机器原因码。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from factory_sop.retention.adapters.dependencies import (
    RetentionHostAuthenticator,
    deletion_ttl_seconds,
    edge_sources,
    evidence_deletion,
    host_authenticator,
    monitor_impact,
    policy_repository,
    station_facts,
    station_overrides,
)
from factory_sop.retention.deletion import (
    EvidenceDeletionRefusedError,
    claim_evidence_deletion,
    settle_evidence_deletion,
    verify_evidence_deletion,
)
from factory_sop.retention.usecases import (
    EdgeImpactSourcesGateway,
    EvidenceDeletionGateway,
    MonitorImpactGateway,
    RetentionPolicyRepository,
    StationFactsGateway,
    StationRetentionOverrideRepository,
)
from factory_sop.settings import Settings
from nvsop_contracts import (
    EvidenceDeletionRequest,
    EvidenceDeletionResultRequest,
    EvidenceDeletionVerifyRequest,
)

router = APIRouter(prefix="/retention/evidence-deletion-claims", tags=["retention"])

# 与 `device.api.INFERENCE_HOST_ID_HEADER` 是同一个 wire 头名；retention 在 device 之下，
# 不能导入它，因此按头名原文声明一次。
_HOST_ID_HEADER = "X-Inference-Host-ID"

HostID = Annotated[UUID, Header(alias=_HOST_ID_HEADER)]
HostTimestamp = Annotated[
    str | None, Header(alias="X-Inference-Host-Timestamp", min_length=1, max_length=32)
]
HostNonce = Annotated[
    str | None, Header(alias="X-Inference-Host-Nonce", min_length=1, max_length=128)
]
HostSignature = Annotated[
    str | None, Header(alias="X-Inference-Host-Signature", min_length=1, max_length=4096)
]
Policies = Annotated[RetentionPolicyRepository, Depends(policy_repository)]
Overrides = Annotated[StationRetentionOverrideRepository, Depends(station_overrides)]
Stations = Annotated[StationFactsGateway, Depends(station_facts)]
Monitor = Annotated[MonitorImpactGateway, Depends(monitor_impact)]
Sources = Annotated[EdgeImpactSourcesGateway, Depends(edge_sources)]
Gateway = Annotated[EvidenceDeletionGateway, Depends(evidence_deletion)]
Authenticate = Annotated[RetentionHostAuthenticator, Depends(host_authenticator)]
TtlSeconds = Annotated[int | None, Depends(deletion_ttl_seconds)]


def _authenticate(
    request: Request,
    authenticate: RetentionHostAuthenticator,
    *,
    host_id: UUID,
    body: dict[str, object],
    now: datetime,
) -> None:
    """请求头身份必须与体内声明的来源主机一致；认证始终执行，没有跳过开关。"""
    if request.headers.get(_HOST_ID_HEADER) != str(host_id):
        raise HTTPException(status_code=403, detail="host_identity_mismatch")
    authenticate(request=request, host_id=host_id, body=body, now=now)


def _refused(error: EvidenceDeletionRefusedError) -> HTTPException:
    return HTTPException(status_code=error.status, detail=error.reason.value)


@router.post("", operation_id="claimEvidenceDeletion", response_model=None)
def claim_deletion_eligibility(
    request: Request,
    response: Response,
    body: dict[str, object],
    host_id: HostID,
    authenticate: Authenticate,
    gateway: Gateway,
    policies: Policies,
    overrides: Overrides,
    stations: Stations,
    monitor: Monitor,
    sources: Sources,
    ttl_seconds: TtlSeconds,
    host_timestamp: HostTimestamp = None,
    host_nonce: HostNonce = None,
    host_signature: HostSignature = None,
) -> dict[str, object] | Response:
    """为一批候选引用判定删除资格；``eligible`` 的条目附带短期票据。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        parsed = EvidenceDeletionRequest.from_wire(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid deletion request") from error
    now = datetime.now(UTC)
    _authenticate(request, authenticate, host_id=host_id, body=body, now=now)
    settings: Settings = request.app.state.settings
    if str(host_id) != parsed.host_id:
        raise HTTPException(status_code=403, detail="host_identity_mismatch")
    try:
        result = claim_evidence_deletion(
            host_id=host_id,
            config_revision=parsed.config_revision,
            effective_sha256=parsed.effective_sha256,
            candidates=parsed.candidates,
            evidence=gateway,
            monitor=monitor,
            stations=stations,
            sources=sources,
            policies=policies,
            overrides=overrides,
            now=now,
            ticket_ttl_seconds=ttl_seconds,
            secret=settings.csrf_secret.get_secret_value(),
        )
    except EvidenceDeletionRefusedError as error:
        raise _refused(error) from error
    return result.to_wire()


@router.post("/verify", operation_id="verifyEvidenceDeletion", response_model=None)
def verify_deletion_eligibility(
    request: Request,
    response: Response,
    body: dict[str, object],
    host_id: HostID,
    authenticate: Authenticate,
    gateway: Gateway,
    policies: Policies,
    overrides: Overrides,
    stations: Stations,
    monitor: Monitor,
    sources: Sources,
    ttl_seconds: TtlSeconds,
    host_timestamp: HostTimestamp = None,
    host_nonce: HostNonce = None,
    host_signature: HostSignature = None,
) -> dict[str, object] | Response:
    """删除前用票据重新校验同一条引用；只有仍为当前事实的认领才继续放行。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        parsed = EvidenceDeletionVerifyRequest.from_wire(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid deletion verify") from error
    now = datetime.now(UTC)
    _authenticate(request, authenticate, host_id=host_id, body=body, now=now)
    settings: Settings = request.app.state.settings
    if str(host_id) != parsed.host_id:
        raise HTTPException(status_code=403, detail="host_identity_mismatch")
    try:
        verified = verify_evidence_deletion(
            host_id=host_id,
            ticket=parsed.ticket,
            config_revision=parsed.config_revision,
            effective_sha256=parsed.effective_sha256,
            candidate=parsed.candidate,
            evidence=gateway,
            monitor=monitor,
            stations=stations,
            sources=sources,
            policies=policies,
            overrides=overrides,
            now=now,
            ticket_ttl_seconds=ttl_seconds,
            secret=settings.csrf_secret.get_secret_value(),
        )
    except EvidenceDeletionRefusedError as error:
        raise _refused(error) from error
    return verified.to_wire()


@router.post("/result", operation_id="reportEvidenceDeletionResult", response_model=None)
def report_deletion_result(
    request: Request,
    response: Response,
    body: dict[str, object],
    host_id: HostID,
    authenticate: Authenticate,
    gateway: Gateway,
    host_timestamp: HostTimestamp = None,
    host_nonce: HostNonce = None,
    host_signature: HostSignature = None,
) -> dict[str, object] | Response:
    """把来源机的终局反馈结案为认领状态；重复同一反馈按同一状态幂等返回。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        parsed = EvidenceDeletionResultRequest.from_wire(body)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid deletion result") from error
    now = datetime.now(UTC)
    _authenticate(request, authenticate, host_id=host_id, body=body, now=now)
    if str(host_id) != parsed.host_id:
        raise HTTPException(status_code=403, detail="host_identity_mismatch")
    try:
        settled = settle_evidence_deletion(
            host_id=host_id,
            claim_id=UUID(parsed.claim_id),
            evidence_id=parsed.evidence_id,
            outcome=parsed.outcome,
            reason_code=parsed.reason_code,
            detail=parsed.detail,
            evidence=gateway,
            now=now,
        )
    except EvidenceDeletionRefusedError as error:
        raise _refused(error) from error
    return settled.to_wire()


__all__ = ["router"]
