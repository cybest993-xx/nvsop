"""再切片执行集成: 真实 FFmpeg/SQLite/本机证据目录 + 委托领取与引用登记 (S038)。"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast

from nvsop_contracts import ReclipEvidenceClaim, ReclipEvidenceCommand, ReclipEvidenceResult
from store_harness import ANCHOR, STATION, opening_state

from edge_runtime.center_client import CenterClient, CenterResponse, CenterUnreachableError
from edge_runtime.evidence_media import (
    EvidenceMediaConfiguration,
    EvidenceMediaWorker,
)
from edge_runtime.evidence_reporting import REGISTRATION_PATH
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Instance, Lifecycle
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import EvidenceSource, LocalState, open_local_state
from edge_runtime.local_state.queues import PendingEvidence
from edge_runtime.supervisor.reclip import (
    ReclipCommandRunner,
    ReclipEvidenceExecutor,
)

FFMPEG, FFPROBE = Path("/usr/bin/ffmpeg"), Path("/usr/bin/ffprobe")
HOST = "019937d8-0d10-7b31-8d2d-4e60c8f4f401"
CAMERA = "camera-a"
_SEGMENTS = [
    "-hide_banner",
    "-loglevel",
    "error",
    "-f",
    "lavfi",
    "-i",
    "testsrc2=size=160x120:rate=10",
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-g",
    "10",
    "-force_key_frames",
    "expr:gte(t,n_forced*2)",
    "-f",
    "segment",
    "-segment_time",
    "2",
    "-reset_timestamps",
    "1",
]


def segments(directory: Path, start: float, count: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    command = [str(FFMPEG), *_SEGMENTS, "-t", str(count * 2)]
    subprocess.run([*command, str(directory / "p%03d.mp4")], check=True)
    for index, part in enumerate(sorted(directory.glob("p*.mp4"))):
        stamp = datetime.fromtimestamp(start + index * 2).strftime("%Y-%m-%d_%H-%M-%S-%f")
        part.rename(directory / f"{stamp}.mp4")


def enqueue(state: LocalState) -> None:
    at = HostInstant(ANCHOR)
    decision = Decision(
        instance_id=1,
        verdict=Verdict.PASS,
        reasons=(),
        violations=(),
        lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
        evidence=EvidenceSpan.at(at),
    )
    instance = Instance(instance_id=1, opened_at=at, last_observation_at=at)
    clip = EvidenceClip(1, at, HostInstant(ANCHOR - 5), HostInstant(ANCHOR + 5))
    state.station(STATION).commit(
        state=opening_state(),
        decisions=(decision,),
        evidence=(clip,),
        closed_instances=(instance,),
        report_provenance={},
        latched_at="2026-10-09T00:00:00+00:00",
        latched_monotonic=HostInstant(1.0),
    )


class FakeCenter:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.posts: list[str] = []
        self.drop_reply = False
        self.status = 200

    def post(self, path: str, payload: dict[str, object]) -> CenterResponse:
        assert path == REGISTRATION_PATH
        evidence_id = str(payload["evidence_id"])
        self.posts.append(evidence_id)
        existing = self.records.setdefault(evidence_id, dict(payload))
        assert existing == payload, "same evidence identity must carry the same payload"
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


class FakeReclipTransport:
    def __init__(self, claims: list[ReclipEvidenceClaim]) -> None:
        self._claims = list(claims)
        self.reports: list[tuple[ReclipEvidenceClaim, ReclipEvidenceResult]] = []

    def claim_next(self) -> ReclipEvidenceClaim | None:
        return self._claims.pop(0) if self._claims else None

    def report(self, claim: ReclipEvidenceClaim, result: ReclipEvidenceResult) -> None:
        self.reports.append((claim, result))


def claim(request_id: str, *, start: float, end: float) -> ReclipEvidenceClaim:
    return ReclipEvidenceClaim(
        command=ReclipEvidenceCommand(
            command_id=f"command-{request_id}",
            request_id=request_id,
            station_id=STATION,
            instance_id=1,
            anchor=ANCHOR,
            window_start=start,
            window_end=end,
        ),
        claim_token=f"token-{request_id}",
        lease_expires_at="2026-10-09T00:05:00Z",
    )


class ReclipExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.recording = self.root / "recordings"
        self.evidence = self.root / "evidence"
        self.center = FakeCenter()
        self.state = self.open_state()
        self.addCleanup(self.state.close)
        segments(self.recording / CAMERA, ANCHOR - 12, 12)
        enqueue(self.state)
        # 首次切片先产出原片段并留下冻结映射; 再切片不依赖它, 但原产物必须保持不变。
        self.assertEqual(self.worker().drain(), 1)

    def open_state(self, name: str = "state.sqlite") -> LocalState:
        return open_local_state(
            str(self.root / name),
            evidence_source=lambda station: (
                EvidenceSource(wall_offset=0.0, media_paths=(CAMERA,))
                if station == STATION
                else None
            ),
        )

    def config(self) -> EvidenceMediaConfiguration:
        return EvidenceMediaConfiguration(self.evidence, FFPROBE, 30.0, None)

    def worker(self, state: LocalState | None = None) -> EvidenceMediaWorker:
        return EvidenceMediaWorker(
            state=state or self.state,
            host_id=HOST,
            recording_directory=self.recording,
            ffmpeg_binary=FFMPEG,
            configuration=self.config(),
            interval=1.0,
        )

    def executor(self, state: LocalState | None = None) -> ReclipEvidenceExecutor:
        return ReclipEvidenceExecutor(
            state=state or self.state,
            host_id=HOST,
            recording_directory=self.recording,
            ffmpeg_binary=FFMPEG,
            configuration=self.config(),
            client=cast(CenterClient, self.center),
            clock=lambda: ANCHOR + 20,
        )

    def row(self, state: LocalState | None = None) -> PendingEvidence:
        return (state or self.state).station(STATION).pending_evidence()[0]

    def original_dir(self) -> Path:
        media = _media(self.row())[0]
        name = cast(str, media["clip_file"])
        return next(d for d in self.evidence.iterdir() if (d / name).is_file())

    def test_reclip_appends_new_generation_with_fixed_anchor_and_union_window(self) -> None:
        original_dir = self.original_dir()
        transport = FakeReclipTransport([claim("req-1", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )

        (report_claim, result) = transport.reports[0]
        self.assertEqual(report_claim.command.request_id, "req-1")
        self.assertIn("req-1", result.evidence_id)
        # 原锚点固定、窗口是并集, 且来源标注为 reclip。
        payloads = [self.center.records[result.evidence_id]]
        clip_payload = next(p for p in payloads if p["kind"] == "clip")
        self.assertEqual(clip_payload["anchor"], ANCHOR)
        self.assertEqual(
            (clip_payload["window_start"], clip_payload["window_end"]),
            (ANCHOR - 8, ANCHOR + 8),
        )
        self.assertEqual(clip_payload["origin"], "reclip")
        self.assertEqual(clip_payload["generation"], "original")
        # 新产物已落盘且可解码; 旧产物目录仍在, 旧引用身份未被覆盖。
        new_dir = next(d for d in self.evidence.iterdir() if d != original_dir and d.is_dir())
        self.assertTrue(original_dir.is_dir())
        clip = new_dir / cast(str, clip_payload["reference"]).split("/")[-1]
        _decode(clip)
        self.assertEqual(
            clip_payload["sha256"],
            hashlib.sha256(clip.read_bytes()).hexdigest(),
        )

    def test_duplicate_claim_and_restart_reuse_one_stable_identity(self) -> None:
        transport = FakeReclipTransport([claim("req-1", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        first_id = transport.reports[0][1].evidence_id
        directories = {d for d in self.evidence.iterdir() if d.is_dir()}

        # 重启后重复领取同一请求: 复用同一产物与同一引用, 不产生冲突代次。
        self.state.close()
        reopened = self.open_state()
        self.addCleanup(reopened.close)
        retry = FakeReclipTransport([claim("req-1", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=retry, executor=self.executor(reopened)).run_once()
        )
        self.assertEqual(retry.reports[0][1].evidence_id, first_id)
        self.assertEqual({d for d in self.evidence.iterdir() if d.is_dir()}, directories)
        # 已定稿且已确认的结果直接复用, 不再重复登记 (首轮每来源 2 条引用)。
        self.assertEqual(len(self.center.posts), 2)

    def test_registration_ack_loss_stays_observable_and_recovers(self) -> None:
        self.center.drop_reply = True
        transport = FakeReclipTransport([claim("req-2", start=ANCHOR - 8, end=ANCHOR + 8)])
        runner = ReclipCommandRunner(transport=transport, executor=self.executor())
        self.assertTrue(runner.run_once())
        # 中心未确认时不回报成功, 本机再切片记录留下可观察原因。
        self.assertEqual(transport.reports, [])
        record = self.state.station(STATION).reclip_request("req-2")
        self.assertIn("CenterUnreachableError", (record.last_error if record else "") or "")
        retry = FakeReclipTransport([claim("req-2", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(ReclipCommandRunner(transport=retry, executor=self.executor()).run_once())
        self.assertEqual(len(retry.reports), 1)
        self.assertIn(retry.reports[0][1].evidence_id, self.center.posts)

    def test_finalized_artifact_is_reused_after_recording_is_gone(self) -> None:
        # 首轮登记确认丢失: 产物已定稿并落盘, 但中心未确认。删除整段滚动录像后重试,
        # 必须直接从本机再切片记录复用已定稿产物, 不再查询/重切已消失的素材。
        self.center.drop_reply = True
        transport = FakeReclipTransport([claim("req-9", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        self.assertEqual(transport.reports, [])
        record = self.state.station(STATION).reclip_request("req-9")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertIsNotNone(record.media_results)
        self.assertIsNone(record.evidence_id)
        finalized = {d for d in self.evidence.iterdir() if d.is_dir()}
        shutil.rmtree(self.recording)
        retry = FakeReclipTransport([claim("req-9", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(ReclipCommandRunner(transport=retry, executor=self.executor()).run_once())
        self.assertEqual(len(retry.reports), 1)
        recovered = self.state.station(STATION).reclip_request("req-9")
        assert recovered is not None
        self.assertEqual(recovered.evidence_id, retry.reports[0][1].evidence_id)
        self.assertIn(recovered.evidence_id, self.center.records)
        self.assertEqual({d for d in self.evidence.iterdir() if d.is_dir()}, finalized)

    def test_window_not_covering_frozen_span_is_rejected_and_observable(self) -> None:
        # 伪造/被裁剪的窗口不含本机冻结锚点时必须拒绝, 不切片也不登记。
        transport = FakeReclipTransport([claim("req-10", start=ANCHOR + 1, end=ANCHOR + 9)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        self.assertEqual(transport.reports, [])
        self.assertEqual(self.center.posts, [])
        record = self.state.station(STATION).reclip_request("req-10")
        assert record is not None
        self.assertIn("reclip_window_incomplete", record.last_error or "")
        self.assertIsNone(record.evidence_id)
        self.assertIsNone(record.media_results)

    def test_missing_frozen_source_is_observable_and_not_registered(self) -> None:
        state = open_local_state(str(self.root / "unmapped.sqlite"))
        self.addCleanup(state.close)
        enqueue(state)
        transport = FakeReclipTransport([claim("req-3", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor(state)).run_once()
        )
        self.assertEqual(transport.reports, [])
        self.assertEqual(self.center.posts, [])


def _media(pending: PendingEvidence) -> list[dict[str, object]]:
    assert pending.media_results is not None
    return cast(list[dict[str, object]], json.loads(pending.media_results))


def _decode(path: Path) -> None:
    subprocess.run(
        [str(FFMPEG), "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        check=True,
    )


if __name__ == "__main__":
    unittest.main()
