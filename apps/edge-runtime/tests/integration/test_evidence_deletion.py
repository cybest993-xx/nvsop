"""#206 Edge 证据删除执行器: 真实 SQLite + 临时证据目录 + durable confirmed 配置。

这里验证的是本机删除边界: 只有持有与当前配置/保护事实一致的资格才取消链接; 中心不可核对、配置
在删前变化、本机出现新的保护或媒体身份变化都跳过并记录稳定原因。中心侧的 claim/verify/result
契约与主机签名 HTTP 边界在 Center 的 `test_evidence_deletion_protection` 中覆盖。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from nvsop_contracts import (
    EvidenceDeletionClaimStatus,
    EvidenceDeletionDecision,
    EvidenceDeletionEntry,
    EvidenceDeletionRequest,
    EvidenceDeletionResponse,
    EvidenceDeletionResultRequest,
    EvidenceDeletionResultResponse,
    EvidenceDeletionVerifyRequest,
    EvidenceDeletionVerifyResponse,
)
from store_harness import STATION, opening_state
from test_confirmed_runtime_composition import _bundle

from edge_runtime.evidence_deletion import (
    EvidenceDeletionExecutor,
    EvidenceDeletionTransportError,
)
from edge_runtime.evidence_media import evidence_artifact_directory, evidence_media_identity
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Instance, Lifecycle
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import open_local_state
from edge_runtime.local_state.configuration import LocalConfigurationStore

HOST = "host-a"


class FakeTransport:
    """内存中心: claim/verify 默认放行, 可注入不可达与删前变化; 记录反馈供断言。"""

    def __init__(self) -> None:
        self.claims: list[EvidenceDeletionRequest] = []
        self.verifies: list[EvidenceDeletionVerifyRequest] = []
        self.reports: list[EvidenceDeletionResultRequest] = []
        self.claim_unreachable = False
        self.verify_unreachable = False
        self.report_unreachable = False
        self.on_verify: Callable[[], None] | None = None

    def claim(self, request: EvidenceDeletionRequest) -> EvidenceDeletionResponse:
        if self.claim_unreachable:
            raise EvidenceDeletionTransportError("unreachable")
        self.claims.append(request)
        entries = tuple(
            EvidenceDeletionEntry(
                evidence_id=candidate.evidence_id,
                station_id=None,
                instance_id=None,
                generation=candidate.generation,
                decision=EvidenceDeletionDecision.ELIGIBLE,
                reason="expired",
                category="pass",
                ticket=f"ticket:{candidate.evidence_id}",
                claim_id=str(uuid4()),
                expires_at=None,
                received_at=None,
                age_seconds=None,
                threshold_seconds=None,
            )
            for candidate in request.candidates
        )
        return EvidenceDeletionResponse(
            host_id=request.host_id,
            config_revision=request.config_revision,
            effective_sha256=request.effective_sha256,
            ticket_ttl_seconds=300,
            entries=entries,
        )

    def verify(self, request: EvidenceDeletionVerifyRequest) -> EvidenceDeletionVerifyResponse:
        if self.verify_unreachable:
            raise EvidenceDeletionTransportError("unreachable")
        self.verifies.append(request)
        if self.on_verify is not None:
            self.on_verify()
        return EvidenceDeletionVerifyResponse(
            claim_id=str(uuid4()),
            evidence_id=request.candidate.evidence_id,
            decision=EvidenceDeletionDecision.ELIGIBLE,
            reason="expired",
            expires_at=None,
        )

    def report(self, request: EvidenceDeletionResultRequest) -> EvidenceDeletionResultResponse:
        if self.report_unreachable:
            raise EvidenceDeletionTransportError("unreachable")
        self.reports.append(request)
        return EvidenceDeletionResultResponse(
            claim_id=request.claim_id,
            evidence_id=request.evidence_id,
            status=EvidenceDeletionClaimStatus(request.outcome.value),
            reason_code=request.reason_code,
        )


class EvidenceDeletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "evidence"
        self.bundle = _bundle()
        self.state = open_local_state(str(self.root / "state.sqlite"))
        self.state.configuration().confirm(self.bundle, confirmed_at=1.0)
        self.transport = FakeTransport()
        self.executor = EvidenceDeletionExecutor(
            state=self.state,
            host_id=HOST,
            evidence_directory=self.directory,
            transport=self.transport,
        )
        self.files = self._seed_registered_evidence()

    def _seed_registered_evidence(self) -> tuple[Path, Path]:
        station = self.state.station(STATION)
        now = HostInstant(100.0)
        station.commit(
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
        (pending,) = station.pending_evidence()
        identity = evidence_media_identity(HOST, pending.queue_id)
        artifact = self.directory / evidence_artifact_directory(identity, 95.0, 105.0)
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
                    "segment_generations": {"original": 1},
                    "sha256": hashlib.sha256(clip.read_bytes()).hexdigest(),
                    "size_bytes": clip.stat().st_size,
                }
            ],
            separators=(",", ":"),
        )
        station.record_evidence_slice(
            pending.queue_id,
            at=HostInstant(110),
            media_results=media_json,
            covered_from=95.0,
            covered_to=105.0,
        )
        station.mark_evidence_registered(
            pending.queue_id, at=HostInstant(120), expected_media_results=media_json
        )
        return clip, still

    def _claim(self) -> EvidenceDeletionRequest:
        return self.transport.claims[0]

    def test_eligible_reference_deletes_media_and_reports_each_file(self) -> None:
        results = self.executor.drain()
        self.assertEqual(
            [(r.outcome, r.reason) for r in results],
            [("deleted", "retention_expired")] * 2,
        )
        self.assertFalse(self.files[0].exists())
        self.assertFalse(self.files[1].exists())
        # 每条文件各自有一条 claim/verify/result, 身份与契约一致。
        claimed = {c.evidence_id for c in self._claim().candidates}
        self.assertEqual(claimed, {v.candidate.evidence_id for v in self.transport.verifies})
        self.assertEqual({r.outcome.value for r in self.transport.reports}, {"deleted"})

    def test_local_protection_beats_a_stale_center_eligible_claim(self) -> None:
        # 中心镜像尚未看到本机新建的待办再切片: 本机在向中心校验前就拒绝删除。
        self.state.station(STATION).record_reclip_intent(
            request_id=str(uuid4()),
            instance_id=1,
            anchor=95.0,
            window_from=94.0,
            window_to=106.0,
            wall_offset=0.0,
            sources=("camera-a",),
            required_from=95.0,
            required_to=105.0,
            identity="reclip-identity",
        )
        results = self.executor.drain()
        self.assertEqual(
            [(r.outcome, r.reason) for r in results],
            [("skipped", "reclip_pending")] * 2,
        )
        self.assertEqual(self.transport.verifies, [])
        self.assertTrue(self.files[0].exists() and self.files[1].exists())
        # 认领被显式结案为 retained, 不留下永久未结案的保护。
        self.assertEqual({r.outcome for r in self.transport.reports}, {"retained"})

    def test_configuration_change_before_unlink_skips_without_deleting(self) -> None:
        def advance() -> None:
            self.state.configuration().confirm(
                replace(self.bundle, config_revision=self.bundle.config_revision + 1),
                confirmed_at=2.0,
            )

        self.transport.on_verify = advance
        results = self.executor.drain()
        self.assertEqual(
            [(r.outcome, r.reason) for r in results],
            [("cancelled", "configuration_changed")] * 2,
        )
        self.assertTrue(self.files[0].exists() and self.files[1].exists())
        self.assertEqual({r.outcome for r in self.transport.reports}, {"cancelled"})

    def test_material_change_before_unlink_skips_without_deleting(self) -> None:
        def tamper() -> None:
            self.files[0].write_bytes(b"tampered after claim")

        self.transport.on_verify = tamper
        results = self.executor.drain()
        outcomes = {r.outcome for r in results}
        self.assertIn("cancelled", outcomes)
        # 被篡改的片段不删; 关键帧身份仍与候选一致, 允许删除。
        self.assertTrue(self.files[0].exists())
        self.assertTrue(
            all(
                r.reason != "retention_expired" or r.evidence_id.endswith("camera-a.jpg")
                for r in results
            )
        )

    def test_unreachable_center_skips_and_deletes_nothing(self) -> None:
        self.transport.claim_unreachable = True
        results = self.executor.drain()
        self.assertEqual(
            [(r.outcome, r.reason) for r in results],
            [("skipped", "center_unreachable")] * 2,
        )
        self.assertTrue(self.files[0].exists() and self.files[1].exists())

    def test_missing_confirmation_skips_and_deletes_nothing(self) -> None:
        with sqlite3.connect(self.root / "state.sqlite") as connection:
            connection.execute("DELETE FROM local_config")
        results = self.executor.drain()
        self.assertEqual(
            [(r.outcome, r.reason) for r in results],
            [("skipped", "configuration_unknown")],
        )
        self.assertTrue(self.files[0].exists() and self.files[1].exists())

    def test_result_report_failure_is_replayed_after_restart(self) -> None:
        self.transport.report_unreachable = True
        results = self.executor.drain()
        # 文件已按当前事实删除, 但结果未送达: 账本保留 result_pending, 不凭缺失文件发明成功。
        self.assertFalse(self.files[0].exists())
        self.assertFalse(self.files[1].exists())
        self.assertEqual({(r.outcome, r.reason) for r in results}, {("skipped", "result_pending")})
        journal = self.state.deletion_journal()
        self.assertEqual({row.stage for row in journal}, {"result_pending"})
        self.assertEqual({row.outcome for row in journal}, {"deleted"})
        # 重启: 新连接/新执行器重放同一结果。
        self.state.close()
        self.state = open_local_state(str(self.root / "state.sqlite"))
        self.transport.report_unreachable = False
        self.executor = EvidenceDeletionExecutor(
            state=self.state,
            host_id=HOST,
            evidence_directory=self.directory,
            transport=self.transport,
        )
        replayed = self.executor.drain()
        self.assertEqual(
            [(r.outcome, r.reason) for r in replayed],
            [("deleted", "retention_expired")] * 2,
        )
        self.assertEqual(self.state.deletion_journal(), ())
        self.assertEqual({r.outcome.value for r in self.transport.reports}, {"deleted"})

    def test_verify_unreachable_converges_on_next_drain(self) -> None:
        self.transport.verify_unreachable = True
        first = self.executor.drain()
        self.assertEqual(
            {(r.outcome, r.reason) for r in first}, {("skipped", "center_unreachable")}
        )
        self.assertTrue(self.files[0].exists() and self.files[1].exists())
        # 认领已领到但未删除: 账本停在 claimed, 使下一轮继续同一条认领而不重复 claim。
        self.assertEqual({row.stage for row in self.state.deletion_journal()}, {"claimed"})
        claims_before = len(self.transport.claims)
        self.transport.verify_unreachable = False
        second = self.executor.drain()
        self.assertEqual(
            [(r.outcome, r.reason) for r in second],
            [("deleted", "retention_expired")] * 2,
        )
        self.assertFalse(self.files[0].exists())
        self.assertFalse(self.files[1].exists())
        self.assertEqual(len(self.transport.claims), claims_before)
        self.assertEqual(self.state.deletion_journal(), ())

    def test_sqlite_write_transaction_blocks_a_second_connection(self) -> None:
        connection = sqlite3.connect(self.root / "state.sqlite", timeout=0.1)
        try:
            second = LocalConfigurationStore(connection)
            with self.state.guard(), self.assertRaises(sqlite3.OperationalError):
                second.confirm(
                    replace(self.bundle, config_revision=self.bundle.config_revision + 1),
                    confirmed_at=2.0,
                )
        finally:
            connection.close()
        # 事务结束后第二个连接仍可写入, 证明阻塞来自真实 BEGIN IMMEDIATE 而非进程内锁。
        with self.state.guard():
            pass
        connection = sqlite3.connect(self.root / "state.sqlite", timeout=0.1)
        try:
            LocalConfigurationStore(connection).confirm(
                replace(self.bundle, config_revision=self.bundle.config_revision + 1),
                confirmed_at=2.0,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
