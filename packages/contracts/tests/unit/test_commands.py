"""推理机委托测试命令的跨进程线契约。"""

from __future__ import annotations

import unittest

from nvsop_contracts import (
    ConnectionTestClaim,
    ConnectionTestCommand,
    ConnectionTestOutcome,
    ConnectionTestResult,
    connection_test_claim_from_wire,
    connection_test_claim_to_wire,
    connection_test_command_from_wire,
    connection_test_command_to_wire,
    connection_test_result_from_wire,
    connection_test_result_to_wire,
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


if __name__ == "__main__":
    unittest.main()
