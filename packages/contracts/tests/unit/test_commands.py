"""推理机委托测试命令的跨进程线契约。"""

from __future__ import annotations

import unittest

from nvsop_contracts import (
    ConnectionTestClaim,
    ConnectionTestCommand,
    ConnectionTestOutcome,
    ConnectionTestResult,
    ExecutionHandoverClaim,
    ExecutionHandoverCommand,
    ExecutionHandoverOutcome,
    ExecutionHandoverResult,
    ReclipEvidenceClaim,
    ReclipEvidenceCommand,
    ReclipEvidenceResult,
    connection_test_claim_from_wire,
    connection_test_claim_to_wire,
    connection_test_command_from_wire,
    connection_test_command_to_wire,
    connection_test_result_from_wire,
    connection_test_result_to_wire,
    execution_handover_claim_from_wire,
    execution_handover_claim_to_wire,
    execution_handover_command_from_wire,
    execution_handover_command_to_wire,
    execution_handover_result_from_wire,
    execution_handover_result_to_wire,
    reclip_evidence_claim_from_wire,
    reclip_evidence_claim_to_wire,
    reclip_evidence_command_from_wire,
    reclip_evidence_command_to_wire,
    reclip_evidence_result_from_wire,
    reclip_evidence_result_to_wire,
)


class DelegatedConnectionContractTest(unittest.TestCase):
    def test_command_round_trips_without_credentials(self) -> None:
        command = ConnectionTestCommand(
            command_id="command-1",
            connector_id="connector-1",
            connector_revision=3,
            connector_type="hikvision_isapi",
            configuration={"address": "10.0.8.21", "port": 80},
        )

        document = connection_test_command_to_wire(command)

        self.assertEqual(command, connection_test_command_from_wire(document))
        self.assertNotIn("password", document)
        self.assertNotIn("username", document)

    def test_result_round_trips_reachable_and_unreachable_answers(self) -> None:
        for result in (
            ConnectionTestResult(
                outcome=ConnectionTestOutcome.REACHABLE,
                credentials_configured=True,
            ),
            ConnectionTestResult(
                outcome=ConnectionTestOutcome.UNREACHABLE,
                detail="credentials rejected",
                credentials_configured=True,
            ),
            ConnectionTestResult(
                outcome=ConnectionTestOutcome.REJECTED,
                detail="local configuration changed",
                failure_code="COMMAND_CONFIGURATION_CHANGED",
            ),
        ):
            with self.subTest(result=result):
                self.assertEqual(
                    result, connection_test_result_from_wire(connection_test_result_to_wire(result))
                )

    def test_non_rejected_result_must_confirm_configured_credentials(self) -> None:
        for outcome in (ConnectionTestOutcome.REACHABLE, ConnectionTestOutcome.UNREACHABLE):
            for credentials_configured in (False, None):
                with (
                    self.subTest(outcome=outcome, credentials_configured=credentials_configured),
                    self.assertRaises(ValueError),
                ):
                    ConnectionTestResult(
                        outcome=outcome,
                        credentials_configured=credentials_configured,
                    )

    def test_unknown_command_fields_are_rejected(self) -> None:
        document = {
            "command_type": "test_connector_connection",
            "command_id": "command-1",
            "connector_id": "connector-1",
            "connector_revision": 1,
            "connector_type": "hikvision_isapi",
            "configuration": {"address": "10.0.8.21"},
            "password": "must-not-cross",  # pragma: allowlist secret
        }

        with self.assertRaises(ValueError):
            connection_test_command_from_wire(document)

    def test_claim_round_trips_as_a_strict_delivery_envelope(self) -> None:
        claim = ConnectionTestClaim(
            command=ConnectionTestCommand(
                command_id="command-1",
                connector_id="connector-1",
                connector_revision=3,
                connector_type="hikvision_isapi",
                configuration={"address": "10.0.8.21"},
            ),
            claim_token="claim-1",
            lease_expires_at="2026-09-08T08:01:00Z",
        )

        document = connection_test_claim_to_wire(claim)

        self.assertEqual(claim, connection_test_claim_from_wire(document))
        self.assertNotIn("password", document)
        self.assertNotIn("username", document)

    def test_claim_rejects_unknown_or_empty_lease_fields(self) -> None:
        document = {
            "command": connection_test_command_to_wire(
                ConnectionTestCommand(
                    command_id="command-1",
                    connector_id="connector-1",
                    connector_revision=1,
                    connector_type="hikvision_isapi",
                    configuration={"address": "10.0.8.21"},
                )
            ),
            "claim_token": "claim-1",
            "lease_expires_at": "2026-09-08T08:01:00Z",
            "password": "must-not-cross",  # pragma: allowlist secret
        }
        with self.assertRaises(ValueError):
            connection_test_claim_from_wire(document)

        document.pop("password")
        document["claim_token"] = ""
        with self.assertRaises(ValueError):
            connection_test_claim_from_wire(document)


class ExecutionHandoverContractTest(unittest.TestCase):
    def command(self) -> ExecutionHandoverCommand:
        return ExecutionHandoverCommand(
            command_id="command-1",
            handover_id="handover-1",
            station_id="station-1",
            from_host_id="host-old",
            to_host_id="host-new",
            grant_id="grant-1",
        )

    def test_command_and_result_round_trip_as_strict_shapes(self) -> None:
        command = self.command()
        claim = ExecutionHandoverClaim(
            command=command,
            claim_token="claim-1",
            lease_expires_at="2026-09-08T08:01:00Z",
        )
        result = ExecutionHandoverResult(
            handover_id="handover-1",
            grant_id="grant-1",
            outcome=ExecutionHandoverOutcome.CONFIRMED,
        )

        self.assertEqual(
            command,
            execution_handover_command_from_wire(execution_handover_command_to_wire(command)),
        )
        self.assertEqual(
            claim, execution_handover_claim_from_wire(execution_handover_claim_to_wire(claim))
        )
        self.assertEqual(
            result, execution_handover_result_from_wire(execution_handover_result_to_wire(result))
        )

    def test_unknown_fields_and_stale_identity_are_rejected(self) -> None:
        document = execution_handover_command_to_wire(self.command())
        with self.assertRaises(ValueError):
            execution_handover_command_from_wire({**document, "request_id": "other"})
        with self.assertRaises(ValueError):
            execution_handover_result_from_wire(
                {
                    "handover_id": "handover-1",
                    "grant_id": "grant-1",
                    "outcome": "confirmed",
                    "detail": None,
                    "failure_code": None,
                    "extra": True,
                }
            )


class ReclipEvidenceContractTest(unittest.TestCase):
    def command(self) -> ReclipEvidenceCommand:
        return ReclipEvidenceCommand(
            command_id="command-1",
            request_id="019937d8-0d10-7b31-8d2d-4e60c8f4f401",
            station_id="019937d8-0d10-7b31-8d2d-4e60c8f4f402",
            instance_id=7,
            anchor=100.0,
            window_start=90.0,
            window_end=110.0,
            available_until=1_700_000_000.0,
        )

    def test_command_round_trips_as_a_strict_object(self) -> None:
        command = self.command()

        document = reclip_evidence_command_to_wire(command)

        self.assertEqual(document["command_type"], "reclip_evidence")
        self.assertEqual(command, reclip_evidence_command_from_wire(document))

    def test_command_rejects_unknown_fields_and_invalid_windows(self) -> None:
        document = reclip_evidence_command_to_wire(self.command())
        with self.assertRaises(ValueError):
            reclip_evidence_command_from_wire({**document, "margins": 5})
        with self.assertRaises(ValueError):
            reclip_evidence_command_from_wire({**document, "window_end": 80.0})
        with self.assertRaises(ValueError):
            ReclipEvidenceCommand(
                command_id="command-1",
                request_id="request-1",
                station_id="station-1",
                instance_id=0,
                anchor=float("nan"),
                window_start=90.0,
                window_end=110.0,
                available_until=1_700_000_000.0,
            )
        # 缺少中心截止的旧形状被拒绝, 来源机不得自行猜归档截止。
        with self.assertRaises(ValueError):
            reclip_evidence_command_from_wire(
                {key: value for key, value in document.items() if key != "available_until"}
            )

    def test_claim_round_trips_and_rejects_empty_lease_fields(self) -> None:
        claim = ReclipEvidenceClaim(
            command=self.command(),
            claim_token="claim-1",
            lease_expires_at="2026-09-08T08:01:00Z",
        )

        document = reclip_evidence_claim_to_wire(claim)

        self.assertEqual(claim, reclip_evidence_claim_from_wire(document))
        with self.assertRaises(ValueError):
            reclip_evidence_claim_from_wire({**document, "claim_token": ""})

    def test_result_carries_only_the_registered_reference_identity(self) -> None:
        result = ReclipEvidenceResult(evidence_id="host-1-reclip-abc/camera-a.mp4")

        document = reclip_evidence_result_to_wire(result)

        self.assertEqual({"evidence_id"}, set(document))
        self.assertEqual(result, reclip_evidence_result_from_wire(document))
        with self.assertRaises(ValueError):
            ReclipEvidenceResult(evidence_id="")

    def test_failure_result_round_trips_without_reusing_the_success_shape(self) -> None:
        result = ReclipEvidenceResult(
            failure_code="material_missing", detail="no recording segment overlaps the window"
        )

        document = reclip_evidence_result_to_wire(result)

        self.assertEqual({"evidence_id", "failure_code", "detail"}, set(document))
        self.assertIsNone(document["evidence_id"])
        self.assertEqual(result, reclip_evidence_result_from_wire(document))
        # 成功与失败互斥: 缺失败原因或同时携带成功引用都必须拒绝, 不静默降级。
        with self.assertRaises(ValueError):
            ReclipEvidenceResult(failure_code="")
        with self.assertRaises(ValueError):
            ReclipEvidenceResult(evidence_id="host-1/camera-a.mp4", failure_code="material_missing")
        with self.assertRaises(ValueError):
            reclip_evidence_result_from_wire({**document, "evidence_id": "host-1/camera-a.mp4"})
        with self.assertRaises(ValueError):
            reclip_evidence_result_from_wire(
                {"evidence_id": None, "failure_code": "", "detail": None}
            )
        with self.assertRaises(ValueError):
            reclip_evidence_result_from_wire(
                {"evidence_id": "host-1/camera-a.mp4", "failure_code": None}
            )


if __name__ == "__main__":
    unittest.main()
