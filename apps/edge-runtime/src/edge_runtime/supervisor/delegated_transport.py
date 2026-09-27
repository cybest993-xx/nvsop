"""supervisor 使用的中心委托命令 HTTP 适配器。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from nvsop_contracts import (
    ConnectionTestClaim,
    ConnectionTestResult,
    connection_test_claim_from_wire,
    connection_test_result_to_wire,
)

from edge_runtime.center_client import CenterClient, CenterResponse, CenterUnreachableError
from edge_runtime.supervisor.delegated_commands import CommandTransport

COMMAND_CLAIM_TOKEN_HEADER = "X-Command-Claim-Token"


class CommandTransportError(RuntimeError):
    """中心命令传输或契约解析失败, 交给外层循环安全重试。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HttpCommandTransport(CommandTransport):
    """通过中心 REST 接口领取和回报委托连接测试命令。"""

    def __init__(self, *, client: CenterClient) -> None:
        self._client = client

    def claim_next(self) -> ConnectionTestClaim | None:
        """领取一条本机命令, 或在队列为空时返回 None。"""
        response = self._call(lambda: self._client.get("/api/v1/device-commands/next"))
        if response.status == 204:
            return None
        if response.status != 200:
            raise CommandTransportError("中心领取命令失败", status=response.status)
        try:
            document = json.loads(response.body.decode("utf-8"))
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
        path = f"/api/v1/device-commands/{claim.command.command_id}/result"
        response = self._call(
            lambda: self._client.post(
                path,
                connection_test_result_to_wire(result),
                headers={COMMAND_CLAIM_TOKEN_HEADER: claim.claim_token},
            )
        )
        if response.status != 200:
            raise CommandTransportError("中心回报命令失败", status=response.status)

    @staticmethod
    def _call(send: Callable[[], CenterResponse]) -> CenterResponse:
        try:
            return send()
        except CenterUnreachableError as error:
            raise CommandTransportError("中心命令接口暂时不可达") from error


__all__ = [
    "COMMAND_CLAIM_TOKEN_HEADER",
    "CommandTransportError",
    "HttpCommandTransport",
]
