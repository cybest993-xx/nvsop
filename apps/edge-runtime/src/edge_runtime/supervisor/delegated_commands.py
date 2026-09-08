"""推理机 supervisor 的中心委托命令执行接缝。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from nvsop_contracts import (
    ConnectionTestClaim,
    ConnectionTestCommand,
    ConnectionTestOutcome,
    ConnectionTestResult,
)


@dataclass(frozen=True, slots=True)
class LocalProbeResult:
    """本机连接器探测的标准结果, 不携带凭据。"""

    outcome: ConnectionTestOutcome
    detail: str | None = None
    failure_code: str | None = None


class LocalConnectorProbe(Protocol):
    """一个已装配连接器的真实健康探测接缝。"""

    def probe(self, /, *, timeout: float) -> LocalProbeResult: ...


@dataclass(frozen=True, slots=True)
class LocalConnector:
    """推理机本地已落盘的连接器配置与真实探测接缝。"""

    revision: int
    credentials_configured: bool
    probe: LocalConnectorProbe


class LocalConnectorRegistry(Protocol):
    """按中心命令的目标 ID 查找本机连接器配置。"""

    def resolve(self, connector_id: str) -> LocalConnector | None: ...


class CommandTransport(Protocol):
    """中心命令传输接缝, 只承载共享领取信封和结果。"""

    def claim_next(self) -> ConnectionTestClaim | None: ...

    def report(self, claim: ConnectionTestClaim, result: ConnectionTestResult) -> None: ...


class ConnectionTestExecutor:
    """在本机真实连接器接缝上执行一个连接测试命令。"""

    def __init__(self, *, registry: LocalConnectorRegistry, timeout: float) -> None:
        if timeout <= 0:
            raise ValueError("connection test timeout must be positive")
        self._registry = registry
        self._timeout = timeout

    def execute(self, command: ConnectionTestCommand) -> ConnectionTestResult:
        """执行命令, 配置不具备安全条件时明确拒绝而不伪造不可达。"""
        connector = self._registry.resolve(command.connector_id)
        if connector is None:
            return ConnectionTestResult(
                outcome=ConnectionTestOutcome.REJECTED,
                detail="推理机未配置该连接器",
                failure_code="COMMAND_TARGET_NOT_FOUND",
            )
        if connector.revision != command.connector_revision:
            return ConnectionTestResult(
                outcome=ConnectionTestOutcome.REJECTED,
                detail="推理机上的连接器配置修订不一致",
                failure_code="COMMAND_CONFIGURATION_CHANGED",
            )
        if not connector.credentials_configured:
            return ConnectionTestResult(
                outcome=ConnectionTestOutcome.REJECTED,
                detail="推理机未配置该连接器凭据",
                credentials_configured=False,
                failure_code="COMMAND_CREDENTIALS_NOT_CONFIGURED",
            )

        probe = connector.probe.probe(timeout=self._timeout)
        if probe.outcome is ConnectionTestOutcome.REJECTED:
            return ConnectionTestResult(
                outcome=ConnectionTestOutcome.REJECTED,
                detail=probe.detail,
                credentials_configured=True,
                failure_code=probe.failure_code or "COMMAND_RESULT_INVALID",
            )
        return ConnectionTestResult(
            outcome=probe.outcome,
            detail=probe.detail,
            credentials_configured=True,
        )


class ConnectionTestCommandRunner:
    """驱动一次中心命令领取、本地真实执行和结果回报。"""

    def __init__(self, *, transport: CommandTransport, executor: ConnectionTestExecutor) -> None:
        self._transport = transport
        self._executor = executor

    def run_once(self) -> bool:
        """处理一条命令; 无命令返回 False, 回报失败则保留异常供外层重试。"""
        claim = self._transport.claim_next()
        if claim is None:
            return False
        result = self._executor.execute(claim.command)
        self._transport.report(claim, result)
        return True


__all__ = [
    "CommandTransport",
    "ConnectionTestCommandRunner",
    "ConnectionTestExecutor",
    "LocalConnector",
    "LocalConnectorProbe",
    "LocalConnectorRegistry",
    "LocalProbeResult",
]
