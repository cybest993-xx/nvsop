"""supervisor 使用的中心委托命令 HTTP 适配器。"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Protocol, cast

from nvsop_contracts import (
    ConnectionTestClaim,
    ConnectionTestResult,
    connection_test_claim_from_wire,
    connection_test_result_to_wire,
)

from edge_runtime.supervisor.delegated_commands import CommandTransport


class _HttpResponse(Protocol):
    """`urlopen` 返回值中本适配器实际使用的最小接口。"""

    status: int

    def read(self) -> bytes: ...

    def close(self) -> None: ...


class CommandTransportError(RuntimeError):
    """中心命令传输或契约解析失败, 交给外层循环安全重试。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HttpCommandTransport(CommandTransport):
    """通过中心 REST 接口领取和回报委托连接测试命令。"""

    def __init__(
        self,
        *,
        center_url: str,
        host_id: str,
        host_token: str,
        timeout: float,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if not center_url:
            raise ValueError("center URL must not be empty")
        if not host_id:
            raise ValueError("inference host ID must not be empty")
        if not host_token:
            raise ValueError("inference host token must not be empty")
        if timeout <= 0:
            raise ValueError("command transport timeout must be positive")
        self._base_url = center_url.rstrip("/")
        self._host_id = host_id
        self._host_token = host_token
        self._timeout = timeout
        self._ssl_context = ssl_context

    def claim_next(self) -> ConnectionTestClaim | None:
        """领取一条本机命令, 或在队列为空时返回 None。"""
        request = urllib.request.Request(
            f"{self._base_url}/api/v1/device-commands/next",
            headers={
                "Accept": "application/json",
                "X-Inference-Host-ID": self._host_id,
                "X-Inference-Host-Token": self._host_token,
            },
            method="GET",
        )
        response = self._open(request)
        if response.status == 204:
            response.close()
            return None
        if response.status != 200:
            status = response.status
            response.close()
            raise CommandTransportError("中心领取命令失败", status=status)
        try:
            raw = response.read()
        finally:
            response.close()
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CommandTransportError("中心领取响应不是有效 JSON") from error
        if not isinstance(document, Mapping):
            raise CommandTransportError("中心领取响应不是对象")
        try:
            return connection_test_claim_from_wire(document)
        except ValueError as error:
            raise CommandTransportError("中心领取响应不符合委托命令契约") from error

    def report(self, claim: ConnectionTestClaim, result: ConnectionTestResult) -> None:
        """回报结果; 中心拒绝时抛出异常而不吞掉租约语义。"""
        payload = json.dumps(
            connection_test_result_to_wire(result),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/v1/device-commands/{claim.command.command_id}/result",
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Inference-Host-ID": self._host_id,
                "X-Inference-Host-Token": self._host_token,
                "X-Command-Claim-Token": claim.claim_token,
            },
            method="POST",
        )
        response = self._open(request)
        try:
            if response.status != 200:
                raise CommandTransportError("中心回报命令失败", status=response.status)
        finally:
            response.close()

    def _open(self, request: urllib.request.Request) -> _HttpResponse:
        """执行一次 HTTP 请求, 统一收敛网络错误且不泄露请求内容。"""
        try:
            return cast(
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
            raise CommandTransportError("中心命令接口拒绝请求", status=status) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise CommandTransportError("中心命令接口暂时不可达") from error


__all__ = ["CommandTransportError", "HttpCommandTransport"]
