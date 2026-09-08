"""推理机委托命令执行器在本地连接器 seam 上的行为。"""

from __future__ import annotations

from dataclasses import dataclass

from nvsop_contracts import (
    ConnectionTestCommand,
    ConnectionTestOutcome,
    ConnectionTestResult,
)

from edge_runtime.supervisor.delegated_commands import (
    ConnectionTestExecutor,
    LocalConnector,
    LocalProbeResult,
)


@dataclass
class FakeConnector:
    """只替换本地 HealthProbe seam, 不代表硬件证据。"""

    answer: LocalProbeResult
    timeouts: list[float]

    def probe(self, /, *, timeout: float) -> LocalProbeResult:
        self.timeouts.append(timeout)
        return self.answer


class FakeRegistry:
    def __init__(self, connector: LocalConnector | None) -> None:
        self.connector = connector

    def resolve(self, connector_id: str) -> LocalConnector | None:
        return self.connector if connector_id == "connector-1" else None


def command(revision: int = 3) -> ConnectionTestCommand:
    return ConnectionTestCommand(
        command_id="command-1",
        connector_id="connector-1",
        connector_revision=revision,
        connector_type="hikvision_isapi",
        configuration={"address": "10.0.8.21", "port": 80},
    )


def test_executor_calls_the_local_real_probe_and_preserves_its_result() -> None:
    probe = FakeConnector(
        answer=LocalProbeResult(outcome=ConnectionTestOutcome.REACHABLE),
        timeouts=[],
    )
    local = LocalConnector(revision=3, credentials_configured=True, probe=probe)
    executor = ConnectionTestExecutor(
        registry=FakeRegistry(local),
        timeout=4.0,
    )

    result = executor.execute(command())

    assert result == ConnectionTestResult(
        outcome=ConnectionTestOutcome.REACHABLE,
        credentials_configured=True,
    )
    assert probe.timeouts == [4.0]


def test_executor_reports_missing_or_stale_local_configuration_as_rejection() -> None:
    missing = ConnectionTestExecutor(registry=FakeRegistry(None), timeout=4.0)
    assert missing.execute(command()) == ConnectionTestResult(
        outcome=ConnectionTestOutcome.REJECTED,
        detail="推理机未配置该连接器",
        failure_code="COMMAND_TARGET_NOT_FOUND",
    )

    probe = FakeConnector(
        answer=LocalProbeResult(outcome=ConnectionTestOutcome.REACHABLE),
        timeouts=[],
    )
    stale = ConnectionTestExecutor(
        registry=FakeRegistry(LocalConnector(revision=2, credentials_configured=True, probe=probe)),
        timeout=4.0,
    )
    assert stale.execute(command(revision=3)) == ConnectionTestResult(
        outcome=ConnectionTestOutcome.REJECTED,
        detail="推理机上的连接器配置修订不一致",
        failure_code="COMMAND_CONFIGURATION_CHANGED",
    )
    assert probe.timeouts == []
