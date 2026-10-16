"""来源机通过已有签名身份逐次回查统计票据;不提供匿名或 Cookie 替代路径。"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from factory_sop.auth.api import SessionFingerprintRecheck
from factory_sop.retention.access import (
    ReportedImpactAccessRefusedError,
    check_reported_impact_context,
)
from factory_sop.retention.adapters.dependencies import (
    RetentionHostAuthenticator,
    host_authenticator,
)
from factory_sop.settings import Settings

router = APIRouter(prefix="/retention/reported-impact", tags=["retention"])


class ImpactAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    context: str


@router.post(
    "/access-check", operation_id="checkRetentionReportedImpactAccess", response_model=None
)
def check_impact_access(
    request: Request,
    response: Response,
    body: ImpactAccessRequest,
    authenticate: Annotated[RetentionHostAuthenticator, Depends(host_authenticator)],
    recheck: SessionFingerprintRecheck,
) -> dict[str, object] | Response:
    response.headers["Cache-Control"] = "no-store"
    try:
        host_id = UUID(request.headers.get("X-Inference-Host-ID", ""))
    except ValueError:
        return Response(
            '{"detail":"host_identity_required"}',
            status_code=403,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    now = datetime.now(UTC)
    authenticate(request=request, host_id=host_id, body=body.model_dump(), now=now)
    settings: Settings = request.app.state.settings
    try:
        query = check_reported_impact_context(
            context=body.context,
            host_id=str(host_id),
            secret=settings.csrf_secret.get_secret_value(),
            now=now.timestamp(),
            resolve_caller=recheck,
        )
    except ReportedImpactAccessRefusedError as error:
        # 原因来自封闭的授权规则,不回显票据、请求内容或底层异常。
        return Response(
            '{"detail":"' + error.code + '"}',
            status_code=error.status,
            media_type="application/json",
            headers={"Cache-Control": "no-store"},
        )
    return query.to_wire()
