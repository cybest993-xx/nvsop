"""推理机调用中心 REST 接口的唯一 HTTP 出口: 主机身份签名、超时、TLS 与网络故障分类。"""

from __future__ import annotations

import json
import secrets
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx2
from nvsop_contracts import HostIdentityRequest, sign_host_identity_request

INFERENCE_HOST_ID_HEADER = "X-Inference-Host-ID"
INFERENCE_HOST_TIMESTAMP_HEADER = "X-Inference-Host-Timestamp"
INFERENCE_HOST_NONCE_HEADER = "X-Inference-Host-Nonce"
INFERENCE_HOST_SIGNATURE_HEADER = "X-Inference-Host-Signature"


class CenterUnreachableError(RuntimeError):
    """连接、超时、断连或半截响应; 调用方按中心暂时不可达处理并留待下一轮。"""


@dataclass(frozen=True, slots=True)
class CenterResponse:
    status: int
    body: bytes


class CenterClient:
    """发送带主机身份签名的请求; 状态码由调用方按各自契约解释。"""

    def __init__(
        self,
        *,
        center_url: str,
        host_id: str,
        host_private_key: str,
        timeout: float,
        ssl_context: ssl.SSLContext | None = None,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        if not center_url or not host_id or not host_private_key:
            raise ValueError("center client identity and URL must not be empty")
        if timeout <= 0:
            raise ValueError("center client timeout must be positive")
        self._base_url = center_url.rstrip("/")
        self._host_id = host_id
        self._host_private_key = host_private_key
        self._timeout = timeout
        # 未配置时沿用标准库默认证书校验, 与原 urllib 行为一致。
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._transport = transport

    def get(self, path: str) -> CenterResponse:
        return self._send("GET", path, body=None, headers={})

    def post(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> CenterResponse:
        return self._send("POST", path, body=body, headers=headers or {})

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None,
        headers: Mapping[str, str],
    ) -> CenterResponse:
        content = (
            None
            if body is None
            else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        request_headers = {
            "Accept": "application/json",
            **({} if content is None else {"Content-Type": "application/json"}),
            **self._signed_headers(method=method, path=path, body=body),
            **headers,
        }
        try:
            with httpx2.Client(
                timeout=self._timeout, verify=self._ssl_context, transport=self._transport
            ) as client:
                response = client.request(
                    method, f"{self._base_url}{path}", content=content, headers=request_headers
                )
        except httpx2.TransportError as error:
            # 不携带底层异常文本, 避免把地址或响应片段写进日志。
            raise CenterUnreachableError("中心接口暂时不可达") from error
        return CenterResponse(status=response.status_code, body=response.content)

    def _signed_headers(
        self, *, method: str, path: str, body: Mapping[str, object] | None
    ) -> dict[str, str]:
        timestamp = int(time.time())
        nonce = secrets.token_urlsafe(18)
        identity_request = HostIdentityRequest(
            method=method,
            path=path,
            host_id=self._host_id,
            timestamp=timestamp,
            nonce=nonce,
            body=body,
        )
        return {
            INFERENCE_HOST_ID_HEADER: self._host_id,
            INFERENCE_HOST_TIMESTAMP_HEADER: str(timestamp),
            INFERENCE_HOST_NONCE_HEADER: nonce,
            INFERENCE_HOST_SIGNATURE_HEADER: sign_host_identity_request(
                identity_request, private_key=self._host_private_key
            ),
        }


__all__ = [
    "INFERENCE_HOST_ID_HEADER",
    "INFERENCE_HOST_NONCE_HEADER",
    "INFERENCE_HOST_SIGNATURE_HEADER",
    "INFERENCE_HOST_TIMESTAMP_HEADER",
    "CenterClient",
    "CenterResponse",
    "CenterUnreachableError",
]
