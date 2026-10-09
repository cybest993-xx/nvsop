"""真实 SQLite + 本机片段: 中心丢确认、重启和多媒体引用对账 (S034)。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast

from store_harness import STATION, opening_state

from edge_runtime.center_client import CenterClient, CenterResponse, CenterUnreachableError
from edge_runtime.evidence_reporting import REGISTRATION_PATH, EvidenceReferenceReconciler
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Instance, Lifecycle
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import EvidenceSource, LocalState, open_local_state

HOST = "019937d8-0d10-7b31-8d2d-4e60c8f4f401"


class FakeCenter:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.posts: list[str] = []
        self.drop_reply = True
        self.status = 200

    def post(self, path: str, payload: dict[str, object]) -> CenterResponse:
        assert path == REGISTRATION_PATH
        evidence_id = str(payload["evidence_id"])
        self.posts.append(evidence_id)
        existing = self.records.setdefault(evidence_id, dict(payload))
        assert existing == payload
        if self.drop_reply:
            self.drop_reply = False
            raise CenterUnreachableError("connection dropped after center registration")
        if self.status != 200:
            return CenterResponse(self.status, b"")
        return CenterResponse(
            200,
            json.dumps(
                {"accepted": True, "evidence_id": evidence_id, "status": "available"}
            ).encode(),
        )


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = str(self.root / "state.sqlite")
        self.directory = self.root / "evidence"
        self.client = FakeCenter()
        self.state = self.open_state()
        self.enqueue()
        self.publish()

    def open_state(self) -> LocalState:
        return open_local_state(
            self.db,
            evidence_source=lambda _: EvidenceSource(wall_offset=0.0, media_paths=("camera-a",)),
        )

    def enqueue(self) -> None:
        now = HostInstant(100.0)
        self.state.station(STATION).commit(
            state=opening_state(),
            decisions=(
                Decision(
                    instance_id=1,
                    verdict=Verdict.PASS,
                    reasons=(),
                    violations=(),
                    lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
                    evidence=EvidenceSpan.at(now),
                ),
            ),
            evidence=(EvidenceClip(1, now, HostInstant(95.0), HostInstant(105.0)),),
            closed_instances=(Instance(1, now, now),),
            report_provenance={},
            latched_at="2026-10-09T00:00:00+00:00",
            latched_monotonic=HostInstant(1),
        )

    def publish(self) -> None:
        station = self.state.station(STATION)
        (pending,) = station.pending_evidence()
        artifact = self.directory / f"{HOST}-{pending.queue_id}-95000-105000"
        artifact.mkdir(parents=True)
        clip = artifact / "camera-a.mp4"
        still = artifact / "camera-a.jpg"
        clip.write_bytes(b"durable local clip")
        still.write_bytes(b"durable local keyframe")
        media_json = json.dumps(
            [
                {
                    "media_path": "camera-a",
                    "clip_file": clip.name,
                    "keyframe_file": still.name,
                    "actual_from": 95.0,
                    "actual_to": 105.0,
                    "material_generation": "original",
                    "sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
                    "size_bytes": clip.stat().st_size,
                }
            ]
        )
        station.record_evidence_slice(
            pending.queue_id,
            at=HostInstant(110),
            media_results=media_json,
            covered_from=95.0,
            covered_to=105.0,
        )
        self.files = (clip, still)

    def worker(self) -> EvidenceReferenceReconciler:
        return EvidenceReferenceReconciler(
            state=self.state,
            client=cast(CenterClient, self.client),
            host_id=HOST,
            evidence_directory=self.directory,
            interval=1.0,
            clock=lambda: 111.0,
        )

    def test_lost_reply_restart_and_duplicate_preserve_both_local_media(self) -> None:
        self.assertEqual(self.worker().drain(limit=5), 0)
        (failed,) = self.state.station(STATION).pending_evidence()
        self.assertIn("CenterUnreachableError", failed.last_error or "")
        self.assertEqual(len(self.client.records), 1)
        self.state.close()
        self.state = self.open_state()
        self.assertEqual(self.worker().drain(limit=5), 1)
        self.assertEqual(len(self.client.records), 2)
        self.assertEqual(len(self.client.posts), 3)
        self.assertEqual(self.worker().drain(limit=5), 0)
        self.assertEqual(len(self.client.posts), 3)
        station = self.state.station(STATION)
        self.assertEqual(station.evidence_ready_for_registration(), ())
        self.assertEqual(len(station.pending_evidence()), 1)
        self.assertTrue(all(path.is_file() for path in self.files))

    def test_center_refusal_keeps_queue_and_registration_does_not_claim_upload(self) -> None:
        self.client.drop_reply = False
        self.client.status = 409
        self.assertEqual(self.worker().drain(limit=5), 0)
        (pending,) = self.state.station(STATION).pending_evidence()
        self.assertIn("HTTP 409", pending.last_error or "")
        self.assertIsNotNone(pending.media_results)
        self.assertEqual((pending.covered_from, pending.covered_to), (95.0, 105.0))
        self.assertTrue(all(path.exists() for path in self.files))
        self.client.status = 200
        self.assertEqual(self.worker().drain(limit=5), 1)
        station = self.state.station(STATION)
        self.assertEqual(station.evidence_ready_for_registration(), ())
        self.assertEqual(len(station.pending_evidence()), 1)
        self.assertTrue(all(path.exists() for path in self.files))


if __name__ == "__main__":
    unittest.main()
