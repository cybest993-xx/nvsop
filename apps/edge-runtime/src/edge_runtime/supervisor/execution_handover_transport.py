"""正常交权停机命令的主机签名 HTTP 传输与生产轮询(#214).

领取只走专用 ``/device-commands/execution-handover/next``: 默认命令端点不认识该 command
type, 旧 Edge 也不会领取到. 回报携带冻结的 handover/grant 身份与 claim token, 由中心在同一
请求事务内复核并结案命令与交权请求.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Protocol

from nvsop_contracts import (
    ExecutionHandoverClaim,
    ExecutionHandoverCommand,
    ExecutionHandoverResult,
    execution_handover_claim_from_wire,
    execution_handover_result_to_wire,
)

from edge_runtime.center_client import CenterClient, CenterResponse, CenterUnreachableError

EXECUTION_HANDOVER_CLAIM_PATH = "/api/v1/device-commands/execution-handover/next"
COMMAND_CLAIM_TOKEN_HEADER = "X-Command-Claim-Token"


class ExecutionHandoverTransportError(RuntimeError):
    """中心命令传输或契约解析失败, 交给外层循环安全重试."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ExecutionHandoverCommandTransport(Protocol):
    """中心正常交权命令的传输接缝, 只承载领取信封和严格结果."""

    def claim_next(self) -> ExecutionHandoverClaim | None: ...

    def report(self, claim: ExecutionHandoverClaim, result: ExecutionHandoverResult) -> None: ...


class HttpExecutionHandoverTransport:
    """通过中心专用签名端点领取和回报正常交权停机命令."""

    def __init__(self, *, client: CenterClient) -> None:
        self._client = client

    def claim_next(self) -> ExecutionHandoverClaim | None:
        response = self._call(lambda: self._client.get(EXECUTION_HANDOVER_CLAIM_PATH))
        if response.status == 204:
            return None
        if response.status != 200:
            raise ExecutionHandoverTransportError(
                "中心领取正常交权命令失败", status=response.status
            )
        try:
            document = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExecutionHandoverTransportError("中心领取响应不是有效 JSON") from error
        if not isinstance(document, Mapping):
            raise ExecutionHandoverTransportError("中心领取响应不是对象")
        try:
            return execution_handover_claim_from_wire(document)
        except ValueError as error:
            raise ExecutionHandoverTransportError("中心领取响应不符合正常交权命令契约") from error

    def report(self, claim: ExecutionHandoverClaim, result: ExecutionHandoverResult) -> None:
        path = f"/api/v1/device-commands/{claim.command.command_id}/execution-handover-result"
        response = self._call(
            lambda: self._client.post(
                path,
                execution_handover_result_to_wire(result),
                headers={COMMAND_CLAIM_TOKEN_HEADER: claim.claim_token},
            )
        )
        if response.status != 200:
            raise ExecutionHandoverTransportError(
                "中心回报正常交权命令失败", status=response.status
            )

    @staticmethod
    def _call(send: Callable[[], CenterResponse]) -> CenterResponse:
        try:
            return send()
        except CenterUnreachableError as error:
            raise ExecutionHandoverTransportError("中心命令接口暂时不可达") from error


class ExecutionHandoverCommandLoop:
    """持续领取、本机真实停机并回报; 中心暂时不可达时安全重试."""

    def __init__(
        self,
        *,
        transport: ExecutionHandoverCommandTransport,
        executor: Callable[[ExecutionHandoverCommand], ExecutionHandoverResult],
        poll_interval: float,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("execution handover poll interval must be positive")
        self._transport = transport
        self._executor = executor
        self._poll_interval = poll_interval
        self._sleep = sleep_fn

    def run_once(self) -> bool:
        """处理一条命令; 无命令返回 False, 传输失败保留异常供外层退避重启."""
        claim = self._transport.claim_next()
        if claim is None:
            return False
        result = self._executor(claim.command)
        self._transport.report(claim, result)
        return True

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        """持续运行, 传输失败只影响本轮并交给下一轮重新领取."""
        while not should_stop():
            with suppress(ExecutionHandoverTransportError):
                self.run_once()
            self._sleep(self._poll_interval)


__all__ = [
    "COMMAND_CLAIM_TOKEN_HEADER",
    "EXECUTION_HANDOVER_CLAIM_PATH",
    "ExecutionHandoverCommandLoop",
    "ExecutionHandoverCommandTransport",
    "ExecutionHandoverTransportError",
    "HttpExecutionHandoverTransport",
]
