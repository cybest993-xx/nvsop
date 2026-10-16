"""#206 Edge 删除资格 HTTP 传输: 精确路径、契约编解码与网络错误分类。"""

from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from uuid import uuid4

from nvsop_contracts import (
    EvidenceDeletionCandidate,
    EvidenceDeletionClaimStatus,
    EvidenceDeletionDecision,
    EvidenceDeletionOutcome,
    EvidenceDeletionRequest,
    EvidenceDeletionResultRequest,
    EvidenceDeletionVerifyRequest,
)

from edge_runtime.center_client import CenterClient, CenterResponse, CenterUnreachableError
from edge_runtime.evidence_deletion import EvidenceDeletionTransportError
from edge_runtime.evidence_deletion_transport import CLAIMS_PATH, HttpEvidenceDeletionTransport

HOST = "019937d8-0d10-7b31-8d2d-4e60c8f4f401"
DIGEST = "a" * 64


class FakeCenterClient(CenterClient):
    """只在测试内替换 post; 构造参数由测试塞入合法占位值。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses: dict[str, CenterResponse] = {}
        self.unreachable = False

    def post(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> CenterResponse:
        del headers
        self.calls.append((path, dict(body)))
        if self.unreachable:
            raise CenterUnreachableError("down")
        return self.responses[path]


def _candidate() -> dict[str, object]:
    return {
        "evidence_id": "artifact/clip.mp4",
        "generation": "original",
        "sha256": DIGEST,
        "size": 10,
        "reference": "artifact/clip.mp4",
    }


class TransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeCenterClient()
        self.transport = HttpEvidenceDeletionTransport(client=self.client, host_id=HOST)
        self.request = EvidenceDeletionRequest(
            host_id=HOST,
            config_revision=7,
            effective_sha256=DIGEST,
            candidates=(),
        )

    def test_claim_verify_and_report_use_the_three_machine_paths(self) -> None:
        # claim 必须携带至少一条候选才能通过共享契约; 这里用同一身份填一条。
        candidate = EvidenceDeletionCandidate.from_wire(_candidate())
        claim = EvidenceDeletionRequest(
            host_id=HOST, config_revision=7, effective_sha256=DIGEST, candidates=(candidate,)
        )
        claim_id = str(uuid4())
        self.client.responses[CLAIMS_PATH] = CenterResponse(
            200,
            json.dumps(
                {
                    "host_id": HOST,
                    "config_revision": 7,
                    "effective_sha256": DIGEST,
                    "ticket_ttl_seconds": 300,
                    "entries": [
                        {
                            "evidence_id": candidate.evidence_id,
                            "station_id": None,
                            "instance_id": None,
                            "generation": "original",
                            "decision": "eligible",
                            "reason": "expired",
                            "category": "pass",
                            "ticket": "signed-ticket",
                            "claim_id": claim_id,
                            "expires_at": None,
                            "received_at": None,
                            "age_seconds": None,
                            "threshold_seconds": None,
                        }
                    ],
                }
            ).encode(),
        )
        response = self.transport.claim(claim)
        self.assertEqual(self.client.calls[-1][0], CLAIMS_PATH)
        self.assertEqual(response.entries[0].decision, EvidenceDeletionDecision.ELIGIBLE)

        self.client.responses[CLAIMS_PATH + "/verify"] = CenterResponse(
            200,
            json.dumps(
                {
                    "claim_id": claim_id,
                    "evidence_id": candidate.evidence_id,
                    "decision": "eligible",
                    "reason": "expired",
                    "expires_at": None,
                }
            ).encode(),
        )
        verified = self.transport.verify(
            EvidenceDeletionVerifyRequest(
                host_id=HOST,
                ticket="signed-ticket",
                config_revision=7,
                effective_sha256=DIGEST,
                candidate=candidate,
            )
        )
        self.assertEqual(self.client.calls[-1][0], CLAIMS_PATH + "/verify")
        self.assertEqual(verified.decision, EvidenceDeletionDecision.ELIGIBLE)

        self.client.responses[CLAIMS_PATH + "/result"] = CenterResponse(
            200,
            json.dumps(
                {
                    "claim_id": claim_id,
                    "evidence_id": candidate.evidence_id,
                    "status": "deleted",
                    "reason_code": "retention_expired",
                }
            ).encode(),
        )
        reported = self.transport.report(
            EvidenceDeletionResultRequest(
                host_id=HOST,
                claim_id=claim_id,
                evidence_id=candidate.evidence_id,
                outcome=EvidenceDeletionOutcome.DELETED,
                reason_code="retention_expired",
                detail=None,
            )
        )
        self.assertEqual(self.client.calls[-1][0], CLAIMS_PATH + "/result")
        self.assertEqual(reported.status, EvidenceDeletionClaimStatus.DELETED)

    def test_non_success_and_unreachable_are_transport_errors(self) -> None:
        self.client.responses[CLAIMS_PATH] = CenterResponse(409, b"")
        with self.assertRaises(EvidenceDeletionTransportError) as rejected:
            self.transport.claim(self.request)
        self.assertEqual(rejected.exception.status, 409)
        self.client.unreachable = True
        with self.assertRaises(EvidenceDeletionTransportError) as unreachable:
            self.transport.claim(self.request)
        self.assertIsNone(unreachable.exception.status)

    def test_host_mismatch_is_refused_before_any_request(self) -> None:
        other = EvidenceDeletionRequest(
            host_id="019937d8-0d10-7b31-8d2d-4e60c8f4f402",
            config_revision=7,
            effective_sha256=DIGEST,
            candidates=(),
        )
        with self.assertRaises(EvidenceDeletionTransportError):
            self.transport.claim(other)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
