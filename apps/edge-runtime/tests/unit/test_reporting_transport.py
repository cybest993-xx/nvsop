from __future__ import annotations

import io
import json
import unittest
import urllib.error
import urllib.request
from email.message import Message
from typing import cast
from unittest.mock import patch

from nvsop_contracts import (
    DECISION_REPORT_CONTRACT_VERSION,
    REPORT_CAPABILITIES_HEADER,
    SOP_INSTANCE_REPORT_CAPABILITY,
    SOP_INSTANCE_REPORT_CONTRACT_VERSION,
    ConfigurationBundle,
    ReportBackendProvenance,
    ReportedDecision,
    ReportEvidence,
)

from edge_runtime.reporting_transport import HttpDecisionReportTransport, ReportTransportError


class JsonResponse:
    status = 200

    def __init__(self, body: dict[str, object] | None = None) -> None:
        self._payload = json.dumps(body or {}).encode("utf-8")
        self.closed = False

    def read(self) -> bytes:
        return self._payload

    def close(self) -> None:
        self.closed = True


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


class ReportCompatibilityTests(unittest.TestCase):
    def transport(self) -> HttpDecisionReportTransport:
        return HttpDecisionReportTransport(
            center_url="http://center.example",
            host_id="host-a",
            host_private_key="unused",  # pragma: allowlist secret
            timeout=1.0,
        )

    def test_v1_report_keeps_old_wire_path_without_handshake(self) -> None:
        requests: list[urllib.request.Request] = []

        def open_request(request: urllib.request.Request, **_: object) -> JsonResponse:
            requests.append(request)
            return JsonResponse()

        with (
            patch(
                "edge_runtime.reporting_transport.sign_host_identity_request",
                return_value="signature",
            ),
            patch(
                "edge_runtime.reporting_transport.urllib.request.urlopen", side_effect=open_request
            ),
        ):
            self.transport().send_decision(v1_report(), configuration=None)

        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].full_url.endswith("/api/v1/monitor/reported-decisions"))
        body = json.loads(cast(bytes, requests[0].data or b"").decode("utf-8"))
        self.assertEqual(body["contract_version"], 1)
        self.assertNotIn("configuration_revision", body)
        self.assertNotIn("backend_provenance", body)

    def test_v2_handshake_precedes_report_and_is_cached_for_same_revision(self) -> None:
        bundle = configuration()
        report = v2_report(bundle)
        requests: list[urllib.request.Request] = []
        responses = iter(
            (
                JsonResponse(
                    {
                        "decision_report_contract_version": DECISION_REPORT_CONTRACT_VERSION,
                        "sop_instance_report_contract_version": (
                            SOP_INSTANCE_REPORT_CONTRACT_VERSION
                        ),
                    }
                ),
                JsonResponse(),
                JsonResponse(),
            )
        )

        def open_request(request: urllib.request.Request, **_: object) -> JsonResponse:
            requests.append(request)
            return next(responses)

        transport = self.transport()
        with (
            patch(
                "edge_runtime.reporting_transport.sign_host_identity_request",
                return_value="signature",
            ),
            patch(
                "edge_runtime.reporting_transport.urllib.request.urlopen", side_effect=open_request
            ),
        ):
            transport.send_decision(report, configuration=bundle)
            transport.send_decision(report, configuration=bundle)

        self.assertEqual(len(requests), 3)
        self.assertTrue(requests[0].full_url.endswith("/confirmed-configuration"))
        handshake_headers = {key.lower(): value for key, value in requests[0].header_items()}
        self.assertEqual(
            handshake_headers[REPORT_CAPABILITIES_HEADER.lower()],
            SOP_INSTANCE_REPORT_CAPABILITY,
        )
        self.assertTrue(requests[1].full_url.endswith("/monitor/reported-decisions"))
        self.assertTrue(requests[2].full_url.endswith("/monitor/reported-decisions"))

    def test_new_edge_does_not_downgrade_v2_when_old_center_lacks_handshake(self) -> None:
        bundle = configuration()
        requests: list[urllib.request.Request] = []

        def old_center(request: urllib.request.Request, **_: object) -> JsonResponse:
            requests.append(request)
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                hdrs=Message(),
                fp=io.BytesIO(b"{}"),
            )

        with (
            patch(
                "edge_runtime.reporting_transport.sign_host_identity_request",
                return_value="signature",
            ),
            patch(
                "edge_runtime.reporting_transport.urllib.request.urlopen", side_effect=old_center
            ),
            self.assertRaises(ReportTransportError) as raised,
        ):
            self.transport().send_decision(v2_report(bundle), configuration=bundle)

        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].full_url.endswith("/confirmed-configuration"))

    def test_v2_requires_center_to_advertise_instance_support_before_decision_post(self) -> None:
        bundle = configuration()
        requests: list[urllib.request.Request] = []

        def incompatible(request: urllib.request.Request, **_: object) -> JsonResponse:
            requests.append(request)
            return JsonResponse(
                {"decision_report_contract_version": DECISION_REPORT_CONTRACT_VERSION}
            )

        with (
            patch(
                "edge_runtime.reporting_transport.sign_host_identity_request",
                return_value="signature",
            ),
            patch(
                "edge_runtime.reporting_transport.urllib.request.urlopen", side_effect=incompatible
            ),
            self.assertRaisesRegex(ReportTransportError, "不受支持"),
        ):
            self.transport().send_decision(v2_report(bundle), configuration=bundle)

        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].full_url.endswith("/confirmed-configuration"))


if __name__ == "__main__":
    unittest.main()
