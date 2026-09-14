"""monitor 判定与健康上报的带签名 HTTP 传输。"""

from __future__ import annotations

import json
import secrets
import ssl
import time
import urllib.error
import urllib.request
from typing import Protocol, cast

from nvsop_contracts import (
    HostIdentityRequest,
    ReportedDecision,
    ReportedHealth,
    reported_decision_to_wire,
    reported_health_to_wire,
    sign_host_identity_request,
)

from edge_runtime.reporting import DecisionReportTransport


class ReportTransportError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HttpDecisionReportTransport(DecisionReportTransport):
    """至少一次 POST 传输;是否重试由本地队列决定。"""

    def __init__(
        self,
        *,
        center_url: str,
        host_id: str,
        host_private_key: str,
        timeout: float,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not center_url or not host_id or not host_private_key:
            raise ValueError("report transport identity and URL must not be empty")
        if timeout <= 0:
            raise ValueError("report transport timeout must be positive")
        self._base_url = center_url.rstrip("/")
        self._host_id = host_id
        self._host_private_key = host_private_key
        self._timeout = timeout
        self._ssl_context = ssl_context

    def send_decision(self, report: ReportedDecision) -> None:
        if report.host_id != self._host_id:
            raise ValueError("a report cannot be sent by a different host")
        self._post("/api/v1/monitor/reported-decisions", reported_decision_to_wire(report))

    def send_health(self, report: ReportedHealth) -> None:
        if report.host_id != self._host_id:
            raise ValueError("a health report cannot be sent by a different host")
        self._post("/api/v1/monitor/health", reported_health_to_wire(report))

    def _post(self, path: str, body: dict[str, object]) -> None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self._signed_headers(path, body),
            },
            method="POST",
        )
        try:
            response = cast(
                _HttpResponse,
                urllib.request.urlopen(
                    request,
                    timeout=self._timeout,
                    context=self._ssl_context,
                ),
            )
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise ReportTransportError("中心 monitor 接口拒绝回报", status=status) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ReportTransportError("中心 monitor 接口暂时不可达") from error
        try:
            if response.status not in {200, 201, 204}:
                raise ReportTransportError(
                    "中心 monitor 接口返回非成功状态", status=response.status
                )
        finally:
            response.close()

    def _signed_headers(self, path: str, body: dict[str, object]) -> dict[str, str]:
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(18)
        request = HostIdentityRequest(
            method="POST",
            path=path,
            host_id=self._host_id,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        return {
            "X-Inference-Host-ID": self._host_id,
            "X-Inference-Host-Timestamp": str(timestamp),
            "X-Inference-Host-Nonce": nonce,
            "X-Inference-Host-Signature": sign_host_identity_request(
                request, private_key=self._host_private_key
            ),
        }


class _HttpResponse(Protocol):
    status: int

    def close(self) -> None: ...


__all__ = ["HttpDecisionReportTransport", "ReportTransportError"]
