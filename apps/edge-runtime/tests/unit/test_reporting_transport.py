from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from unittest.mock import patch

import httpx2
from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    HEALTH_REPORT_CAPABILITY,
    HEALTH_REPORT_CONTRACT_VERSION,
    REPORT_CAPABILITIES_HEADER,
    SOP_INSTANCE_REPORT_CAPABILITY,
    SOP_INSTANCE_REPORT_CONTRACT_VERSION,
    ConfigurationBundle,
    ReportBackendProvenance,
    ReportedDecision,
    ReportedExecutionAuthority,
    ReportedHealth,
    ReportedObservation,
    ReportEvidence,
)

from edge_runtime.center_client import CenterClient
from edge_runtime.reporting_transport import HttpDecisionReportTransport, ReportTransportError

_HANDSHAKE_OK = {
    "decision_report_contract_version": DECISION_REPORT_CONTRACT_VERSION,
    "sop_instance_report_contract_version": SOP_INSTANCE_REPORT_CONTRACT_VERSION,
    "health_report_contract_version": HEALTH_REPORT_CONTRACT_VERSION,
}


def configuration() -> ConfigurationBundle:
    return ConfigurationBundle(
        host_id="host-a",
        config_revision=7,
        generated_at="2026-09-16T00:00:00Z",
        stations=(),
    )


def v1_report() -> ReportedDecision:
    return ReportedDecision(
        event_id="host-a:v1",
        trace_id="host-a:v1",
        host_id="host-a",
        station_id="station-a",
        backend_id="backend-a",
        instance_id=1,
        verdict="pass",
        reason_codes=(),
        violations=(),
        lifecycle="closed_by_end_signal",
        evidence=ReportEvidence(None, None, None),
        template_version_id=None,
        template_sha256=None,
        model_ids=("model-a",),
        reported_at="2026-09-16T00:00:00Z",
    )


def v2_report(bundle: ConfigurationBundle) -> ReportedDecision:
    return ReportedDecision(
        event_id="host-a:v2",
        trace_id="host-a:v2",
        host_id="host-a",
        station_id="station-a",
        backend_id=None,
        instance_id=2,
        verdict="pass",
        reason_codes=(),
        violations=(),
        lifecycle="closed_by_end_signal",
        evidence=ReportEvidence(None, None, None),
        template_version_id=None,
        template_sha256=None,
        model_ids=(),
        reported_at="2026-09-16T00:00:00Z",
        backend_provenance=(ReportBackendProvenance("backend-b", ("model-b",)),),
        configuration_revision=bundle.config_revision,
        configuration_sha256=bundle.effective_sha256,
        contract_version=DECISION_REPORT_CONTRACT_VERSION,
    )


def execution_authority_report() -> ReportedExecutionAuthority:
    return ReportedExecutionAuthority(
        host_id="host-a",
        station_id="station-a",
        authority_state="active",
        write_state="enabled",
        reason_code=None,
        detail=None,
        grant_id="grant-a",
        holder_host_id="host-a",
        lease_expires_at="2026-10-10T00:00:00Z",
        renewed_at="2026-10-09T00:00:00Z",
        reported_at="2026-10-09T12:00:00Z",
    )


def health_report() -> ReportedHealth:
    return ReportedHealth(
        event_id="host-a:health:1",
        trace_id="host-a:health:1",
        host_id="host-a",
        station_id="station-a",
        stream_id="camera-a",
        status="source_error",
        reason_code="STREAM_LOST",
        detail=None,
        occurred_at="2026-09-16T00:00:00Z",
        source_anchor=None,
        anchor_offset=None,
        reported_at="2026-09-16T00:00:00Z",
    )


class ReportCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        signing = patch(
            "edge_runtime.center_client.sign_host_identity_request", return_value="signature"
        )
        signing.start()
        self.addCleanup(signing.stop)
        self.requests: list[httpx2.Request] = []

    def transport(
        self, respond: Callable[[httpx2.Request], httpx2.Response]
    ) -> HttpDecisionReportTransport:
        def record(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            return respond(request)

        client = CenterClient(
            center_url="http://center.example",
            host_id="host-a",
            host_private_key="unused",  # pragma: allowlist secret
            timeout=1.0,
            transport=httpx2.MockTransport(record),
        )
        return HttpDecisionReportTransport(client=client, host_id="host-a")

    def test_observation_report_posts_without_a_handshake(self) -> None:
        report = ReportedObservation(
            event_id="host-a:observation:1",
            trace_id="host-a:observation:1",
            host_id="host-a",
            station_id="station-a",
            instance_id=1,
            source="action",
            signal="(1) step 1",
            source_time=0.5,
            source_anchor=100.0,
            observed_at=12.0,
            template_version_id=None,
            template_sha256=None,
            backend=None,
            reported_at="2026-09-16T00:00:00Z",
        )

        self.transport(lambda _: httpx2.Response(200, json={})).send_observation(report)

        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/api/v1/monitor/reported-observations")
        body = json.loads(self.requests[0].content.decode("utf-8"))
        self.assertEqual(body["event_id"], report.event_id)
        self.assertEqual(body["source"], "action")

    def test_v1_report_keeps_old_wire_path_without_handshake(self) -> None:
        self.transport(lambda _: httpx2.Response(200, json={})).send_decision(
            v1_report(), configuration=None
        )

        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/api/v1/monitor/reported-decisions")
        body = json.loads(self.requests[0].content.decode("utf-8"))
        self.assertEqual(body["contract_version"], 1)
        self.assertNotIn("configuration_revision", body)
        self.assertNotIn("backend_provenance", body)

    def test_v2_handshake_precedes_report_and_is_cached_for_same_revision(self) -> None:
        bundle = configuration()
        report = v2_report(bundle)
        responses = iter(
            (
                httpx2.Response(200, json=_HANDSHAKE_OK),
                httpx2.Response(200, json={}),
                httpx2.Response(200, json={}),
            )
        )
        transport = self.transport(lambda _: next(responses))

        transport.send_decision(report, configuration=bundle)
        transport.send_decision(report, configuration=bundle)

        self.assertEqual(len(self.requests), 3)
        self.assertTrue(self.requests[0].url.path.endswith("/confirmed-configuration"))
        self.assertEqual(
            self.requests[0].headers[REPORT_CAPABILITIES_HEADER],
            f"{SOP_INSTANCE_REPORT_CAPABILITY},{HEALTH_REPORT_CAPABILITY}",
        )
        self.assertEqual(self.requests[1].url.path, "/api/v1/monitor/reported-decisions")
        self.assertEqual(self.requests[2].url.path, "/api/v1/monitor/reported-decisions")

    def test_signed_envelope_preserves_report_and_older_center_archives_without_live(self) -> None:
        bundle = configuration()
        report = v2_report(bundle)
        sent = iter(
            (
                httpx2.Response(200, json=_HANDSHAKE_OK),
                httpx2.Response(404, json={}),
                httpx2.Response(200, json={}),
            )
        )
        self.transport(lambda _: next(sent)).send_timed_decision(
            report,
            configuration=bundle,
            latched_at="2026-10-09T06:00:00Z",
            realtime=True,
        )
        assert self.requests[1].url.path.endswith("/reported-decisions/enveloped")
        envelope = json.loads(self.requests[1].content)
        assert envelope == {
            "decision": report.to_wire(),
            "latched_at": "2026-10-09T06:00:00Z",
            "realtime": True,
        }
        assert self.requests[2].url.path.endswith("/reported-decisions")
        assert json.loads(self.requests[2].content) == report.to_wire()

    def test_new_edge_does_not_downgrade_v2_when_old_center_lacks_handshake(self) -> None:
        bundle = configuration()

        with self.assertRaises(ReportTransportError) as raised:
            self.transport(lambda _: httpx2.Response(404, json={})).send_decision(
                v2_report(bundle), configuration=bundle
            )

        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0].url.path.endswith("/confirmed-configuration"))

    def test_v2_requires_center_to_advertise_instance_support_before_decision_post(self) -> None:
        bundle = configuration()
        incompatible = {"decision_report_contract_version": DECISION_REPORT_CONTRACT_VERSION}

        with self.assertRaisesRegex(ReportTransportError, "不受支持"):
            self.transport(lambda _: httpx2.Response(200, json=incompatible)).send_decision(
                v2_report(bundle), configuration=bundle
            )

        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0].url.path.endswith("/confirmed-configuration"))

    def test_health_report_negotiates_the_health_contract_before_posting(self) -> None:
        bundle = configuration()
        responses = iter(
            (
                httpx2.Response(200, json=_HANDSHAKE_OK),
                httpx2.Response(200, json={}),
            )
        )
        transport = self.transport(lambda _: next(responses))

        transport.send_health(health_report(), configuration=bundle)

        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.requests[0].url.path.endswith("/confirmed-configuration"))
        self.assertEqual(self.requests[1].url.path, "/api/v1/monitor/health")

    def test_execution_authority_posts_as_signed_advisory_state(self) -> None:
        transport = self.transport(lambda _: httpx2.Response(200, json={}))

        transport.send_execution_authority(execution_authority_report())

        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/api/v1/monitor/execution-authority")
        self.assertEqual(
            json.loads(self.requests[0].content.decode("utf-8"))["write_state"], "enabled"
        )

    def test_old_center_404_disables_only_execution_authority_advisory_report(self) -> None:
        transport = self.transport(lambda _: httpx2.Response(404, json={}))

        transport.send_execution_authority(execution_authority_report())
        transport.send_execution_authority(execution_authority_report())

        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/api/v1/monitor/execution-authority")

    def test_health_report_without_a_frozen_configuration_posts_directly(self) -> None:
        transport = self.transport(lambda _: httpx2.Response(200, json={}))

        transport.send_health(health_report(), configuration=None)

        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0].url.path, "/api/v1/monitor/health")


if __name__ == "__main__":
    unittest.main()
