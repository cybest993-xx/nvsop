"""只读 Edge 统计的独立短票据;不复用证据媒体授权,不授权任何写入。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable

from factory_sop.auth.api import Caller, Permission, authorize
from nvsop_contracts.retention_impact import ReportedImpactQuery

CONTEXT_LIMIT = 4096
_SCOPE = b"retention-reported-impact\0"


class ReportedImpactAccessRefusedError(ValueError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status, self.code = status, code


def issue_reported_impact_context(
    caller: Caller,
    *,
    query: ReportedImpactQuery,
    fingerprint: str,
    secret: str,
    now: float,
    ttl_seconds: int,
) -> str:
    """授权用户发起的精确查询,有效期来自所属保留确认配置。"""
    authorize(caller, Permission.RETENTION_POLICY_EDIT)
    payload = json.dumps(
        {"query": query.to_wire(), "fingerprint": fingerprint, "expires_at": now + ttl_seconds},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret.encode(), _SCOPE + encoded.encode(), hashlib.sha256).hexdigest()
    context = encoded + "." + signature
    if len(context) > CONTEXT_LIMIT:
        raise ReportedImpactAccessRefusedError(409, "query_scope_too_large")
    return context


def check_reported_impact_context(
    *,
    context: str,
    host_id: str,
    secret: str,
    now: float,
    resolve_caller: Callable[[str], Caller | None],
) -> ReportedImpactQuery:
    """签名主机逐次回查:校验作用域/来源/时限并重读当前会话与权限。"""
    try:
        if len(context) > CONTEXT_LIMIT:
            raise ValueError
        encoded, signature = context.split(".")
        expected = hmac.new(secret.encode(), _SCOPE + encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        if not isinstance(claims, dict) or set(claims) != {"query", "fingerprint", "expires_at"}:
            raise ValueError
        if not isinstance(claims["query"], dict):
            raise ValueError
        query = ReportedImpactQuery.from_wire(claims["query"])
        fingerprint = claims["fingerprint"]
        expires = claims["expires_at"]
        if not isinstance(fingerprint, str) or not fingerprint or type(expires) not in (int, float):
            raise ValueError
        if query.host_id != host_id:
            raise ValueError
    except (ValueError, TypeError, UnicodeError) as error:
        raise ReportedImpactAccessRefusedError(403, "access_denied") from error
    if now >= expires:
        raise ReportedImpactAccessRefusedError(410, "context_expired")
    caller = resolve_caller(fingerprint)
    if caller is None:
        raise ReportedImpactAccessRefusedError(403, "session_invalid")
    authorize(caller, Permission.RETENTION_POLICY_EDIT)
    return query
