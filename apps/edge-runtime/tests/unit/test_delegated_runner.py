"""推理机领取、真实执行和结果回报的编排接缝。"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from nvsop_contracts import (
    ConnectionTestClaim,
    ConnectionTestCommand,
    ConnectionTestOutcome,
    ConnectionTestResult,
)

from edge_runtime.supervisor.delegated_commands import (
    ConnectionTestCommandRunner,
    ConnectionTestExecutor,
    LocalConnector,
    LocalProbeResult,
)


@dataclass
class FakeProbe:
    result: LocalProbeResult
    timeouts: list[float]

    def probe(self, /, *, timeout: float) -> LocalProbeResult:
        self.timeouts.append(timeout)
        return self.result


class Registry:
    def __init__(self, connector: LocalConnector) -> None:
        self.connector = connector

    def resolve(self, connector_id: str) -> LocalConnector | None:
        return self.connector if connector_id == "connector-1" else None


class Transport:
    def __init__(self, claim: ConnectionTestClaim | None) -> None:
        self.claim = claim
        self.reported: list[tuple[ConnectionTestClaim, ConnectionTestResult]] = []

    def claim_next(self) -> ConnectionTestClaim | None:
        claim = self.claim
        self.claim = None
        return claim

    def report(self, claim: ConnectionTestClaim, result: ConnectionTestResult) -> None:
        self.reported.append((claim, result))


def claim() -> ConnectionTestClaim:
    return ConnectionTestClaim(
        command=ConnectionTestCommand(
            command_id="command-1",
            connector_id="connector-1",
            connector_revision=3,
            connector_type="hikvision_isapi",
            configuration={"address": "10.0.8.21", "port": 80},
        ),
        claim_token="claim-1",
        lease_expires_at="2026-09-08T08:01:00Z",
    )


class DelegatedCommandRunnerTest(unittest.TestCase):
    def test_runner_executes_the_claimed_command_and_reports_the_real_result(self) -> None:
        probe = FakeProbe(
            result=LocalProbeResult(outcome=ConnectionTestOutcome.REACHABLE),
            timeouts=[],
        )
        executor = ConnectionTestExecutor(
            registry=Registry(LocalConnector(revision=3, credentials_configured=True, probe=probe)),
            timeout=4.0,
        )
        transport = Transport(claim())
        runner = ConnectionTestCommandRunner(transport=transport, executor=executor)

        self.assertTrue(runner.run_once())

        self.assertEqual(
            [
                (
                    claim(),
                    ConnectionTestResult(
                        outcome=ConnectionTestOutcome.REACHABLE, credentials_configured=True
                    ),
                )
            ],
            transport.reported,
        )
        self.assertEqual([4.0], probe.timeouts)
        self.assertFalse(runner.run_once())

    def test_runner_does_not_report_when_the_center_has_no_command(self) -> None:
        transport = Transport(None)
        executor = ConnectionTestExecutor(
            registry=Registry(
                LocalConnector(
                    revision=3,
                    credentials_configured=True,
                    probe=FakeProbe(
                        result=LocalProbeResult(outcome=ConnectionTestOutcome.REACHABLE),
                        timeouts=[],
                    ),
                )
            ),
            timeout=4.0,
        )

        self.assertFalse(
            ConnectionTestCommandRunner(transport=transport, executor=executor).run_once()
        )
        self.assertEqual([], transport.reported)


if __name__ == "__main__":
    unittest.main()
