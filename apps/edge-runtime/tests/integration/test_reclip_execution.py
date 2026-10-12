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
from unittest.mock import patch

from nvsop_contracts import ReclipEvidenceClaim, ReclipEvidenceCommand, ReclipEvidenceResult
from store_harness import ANCHOR, STATION, opening_state

from edge_runtime.center_client import CenterClient, CenterResponse, CenterUnreachableError
from edge_runtime.evidence_media import (
    EvidenceMediaConfiguration,
    EvidenceMediaWorker,
    reclip_evidence_identity,
)
from edge_runtime.evidence_reporting import REGISTRATION_PATH
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Instance, Lifecycle
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import EvidenceSource, LocalState, StationStore, open_local_state
from edge_runtime.local_state.queues import PendingEvidence
from edge_runtime.runtime import ReclipCommandLoop, build_reclip_loop
from edge_runtime.supervisor.reclip import (
    ReclipCommandRunner,
    ReclipEvidenceExecutor,
)

FFMPEG, FFPROBE = Path("/usr/bin/ffmpeg"), Path("/usr/bin/ffprobe")
HOST = "019937d8-0d10-7b31-8d2d-4e60c8f4f401"
CAMERA = "camera-a"
# 合成墙钟基准; 测试通过它控制中心截止是否已过。
WALL_NOW = 1_700_000_000.0
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


def claim(
    request_id: str,
    *,
    start: float,
    end: float,
    instance_id: int = 1,
    available_until: float = WALL_NOW + 1000.0,
) -> ReclipEvidenceClaim:
    return ReclipEvidenceClaim(
        command=ReclipEvidenceCommand(
            command_id=f"command-{request_id}",
            request_id=request_id,
            station_id=STATION,
            instance_id=instance_id,
            anchor=ANCHOR,
            window_start=start,
            window_end=end,
            available_until=available_until,
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
            wall_clock=lambda: WALL_NOW,
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
        result_evidence_id = result.evidence_id
        assert result_evidence_id is not None
        self.assertIn("req-1", result_evidence_id)
        # 原锚点固定、窗口是并集, 且来源标注为 reclip。
        payloads = [self.center.records[result_evidence_id]]
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

    def test_center_registration_rejection_waits_for_recovery(self) -> None:
        # 中心在重启窗口拒绝登记 (503): 这是等待恢复, 不是不可恢复的素材过期 (Issue 201)。
        # 基线行为: 任何未成功的中心登记都保留本机待办下一轮重试, 不结案成终局失败。
        self.center.status = 503
        transport = FakeReclipTransport([claim("req-503", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        # 中心拒绝时不回报终局失败, 本机再切片记录留下可观察原因等待恢复。
        self.assertEqual(transport.reports, [])
        record = self.state.station(STATION).reclip_request("req-503")
        assert record is not None
        self.assertIsNone(record.terminal_error_code)
        self.assertIn("HTTP 503", record.last_error or "")
        self.assertIsNone(record.evidence_id)
        # 中心恢复后同一待办重试即可登记成功, 复用已定稿产物, 不重切也不新建代次。
        self.center.status = 200
        retry = FakeReclipTransport([claim("req-503", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(ReclipCommandRunner(transport=retry, executor=self.executor()).run_once())
        self.assertEqual(len(retry.reports), 1)
        self.assertIn(retry.reports[0][1].evidence_id, self.center.posts)
        recovered = self.state.station(STATION).reclip_request("req-503")
        assert recovered is not None
        self.assertIsNone(recovered.terminal_error_code)

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
        # 伪造/被裁剪的窗口不含本机冻结锚点时必须拒绝, 不切片, 也不登记中心引用。
        transport = FakeReclipTransport([claim("req-10", start=ANCHOR + 1, end=ANCHOR + 9)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        # 终局失败必须回报中心结案, 且带具体可查询原因 (S039/AC1)。
        self.assertEqual(len(transport.reports), 1)
        self.assertEqual(transport.reports[0][1].failure_code, "reclip_window_incomplete")
        self.assertEqual(self.center.posts, [])
        record = self.state.station(STATION).reclip_request("req-10")
        assert record is not None
        self.assertIn("reclip_window_incomplete", record.last_error or "")
        self.assertIsNone(record.evidence_id)
        self.assertIsNone(record.media_results)

    def test_missing_archived_material_reports_a_terminal_failure(self) -> None:
        # 排队等待期间滚动录像被回收: 素材缺失是终局失败, 必须回报具体原因 (S039/AC1)。
        shutil.rmtree(self.recording)
        transport = FakeReclipTransport([claim("req-11", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        self.assertEqual(len(transport.reports), 1)
        self.assertEqual(transport.reports[0][1].failure_code, "material_missing")
        self.assertIsNone(transport.reports[0][1].evidence_id)
        self.assertEqual(self.center.posts, [])
        record = self.state.station(STATION).reclip_request("req-11")
        assert record is not None
        self.assertIn("material_missing", record.last_error or "")
        self.assertEqual(record.terminal_error_code, "material_missing")
        self.assertIsNone(record.evidence_id)
        self.assertIsNone(record.media_results)

    def test_terminal_failure_is_frozen_across_restart_and_restored_material(self) -> None:
        # 排队期间素材缺失 -> 终局失败; 即使之后素材恢复/重复领取, 也复用同一原因, 不重切 (AC1)。
        shutil.rmtree(self.recording)
        transport = FakeReclipTransport([claim("req-12", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        self.assertEqual(transport.reports[0][1].failure_code, "material_missing")
        frozen = self.state.station(STATION).reclip_request("req-12")
        assert frozen is not None
        self.assertEqual(frozen.terminal_error_code, "material_missing")

        # 素材后来恢复: 重启后重复领取仍复用冻结原因, 不产生新产物也不改原因。
        segments(self.recording / CAMERA, ANCHOR - 12, 12)
        self.state.close()
        reopened = self.open_state()
        self.addCleanup(reopened.close)
        retry = FakeReclipTransport([claim("req-12", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=retry, executor=self.executor(reopened)).run_once()
        )
        self.assertEqual(len(retry.reports), 1)
        self.assertEqual(retry.reports[0][1].failure_code, "material_missing")
        self.assertIsNone(retry.reports[0][1].evidence_id)
        self.assertEqual(self.center.posts, [])
        recovered = reopened.station(STATION).reclip_request("req-12")
        assert recovered is not None
        self.assertEqual(recovered.terminal_error_code, "material_missing")
        self.assertIsNone(recovered.evidence_id)
        self.assertIsNone(recovered.media_results)

    def test_expired_archive_reports_a_terminal_failure_before_slicing(self) -> None:
        # 中心截止已过且尚未产出任何产物: 明确报归档过期, 不切片 (AC1)。
        transport = FakeReclipTransport(
            [claim("req-expired", start=ANCHOR - 8, end=ANCHOR + 8, available_until=WALL_NOW - 1.0)]
        )
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        self.assertEqual(len(transport.reports), 1)
        self.assertEqual(transport.reports[0][1].failure_code, "archive_expired")
        self.assertEqual(self.center.posts, [])
        record = self.state.station(STATION).reclip_request("req-expired")
        assert record is not None
        self.assertEqual(record.terminal_error_code, "archive_expired")
        self.assertIsNone(record.media_results)

    def test_expired_archive_still_reuses_a_finalized_artifact(self) -> None:
        # 已定稿产物优先于过期判定: 登记确认丢失后即使中心截止已过, 也复用产物而不报过期。
        self.center.drop_reply = True
        transport = FakeReclipTransport([claim("req-final", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        self.assertEqual(transport.reports, [])
        record = self.state.station(STATION).reclip_request("req-final")
        assert record is not None and record.media_results is not None
        shutil.rmtree(self.recording)
        retry = FakeReclipTransport(
            [claim("req-final", start=ANCHOR - 8, end=ANCHOR + 8, available_until=WALL_NOW - 1.0)]
        )
        self.assertTrue(ReclipCommandRunner(transport=retry, executor=self.executor()).run_once())
        self.assertEqual(len(retry.reports), 1)
        self.assertIsNotNone(retry.reports[0][1].evidence_id)
        recovered = self.state.station(STATION).reclip_request("req-final")
        assert recovered is not None
        self.assertIsNone(recovered.terminal_error_code)

    def test_reclip_command_loop_runs_the_runner_each_tick(self) -> None:
        transport = FakeReclipTransport([claim("req-loop", start=ANCHOR - 8, end=ANCHOR + 8)])
        loop = ReclipCommandLoop(
            runner=ReclipCommandRunner(transport=transport, executor=self.executor()),
            poll_interval=0.001,
        )
        self.assertTrue(loop.run_once())
        self.assertEqual(len(transport.reports), 1)
        self.assertEqual(transport.reports[0][1].failure_code, None)
        # 无待办时 run_forever 也不报错, 由外层 should_stop 结束本轮。
        rounds = iter(range(3))
        loop.run_forever(should_stop=lambda: next(rounds, None) is None)

    def test_build_reclip_loop_composes_the_production_tick(self) -> None:
        loop = build_reclip_loop(
            center_url="http://127.0.0.1:1",
            host_id=HOST,
            host_private_key="unused",  # pragma: allowlist secret
            command_timeout=1.0,
            command_poll_interval=1.0,
            state=self.state,
            recording_directory=self.recording,
            ffmpeg_binary=FFMPEG,
            evidence_configuration=self.config(),
        )
        self.assertIsInstance(loop, ReclipCommandLoop)

    def test_internal_recording_gap_is_terminal_not_reported_success(self) -> None:
        # 同源录像内部有缺口 (删除中间分段): 拼接片段会短于请求窗口, 必须终局失败 (AC3)。
        # `_select_segments` 对窗口内缺口报 material_missing; 本票不改变首次切片的允许部分覆盖语义。
        directory = self.recording / CAMERA
        middle = sorted(directory.glob("*.mp4"))[6]
        middle.unlink()
        transport = FakeReclipTransport([claim("req-gap", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        self.assertEqual(len(transport.reports), 1)
        self.assertEqual(transport.reports[0][1].failure_code, "material_missing")
        self.assertEqual(self.center.posts, [])
        record = self.state.station(STATION).reclip_request("req-gap")
        assert record is not None
        self.assertEqual(record.terminal_error_code, "material_missing")
        self.assertIsNone(record.evidence_id)
        self.assertIsNone(record.media_results)

    def test_partial_frozen_sources_are_terminal_not_reported_success(self) -> None:
        # 冻结来源有两路相机, 已定稿产物只覆盖一路: 不得冒充成功 (AC3)。
        station = self.state.station(STATION)
        row = station.evidence_at(instance_id=1, anchor=ANCHOR)
        assert row is not None and row.wall_offset is not None
        identity = reclip_evidence_identity(HOST, row.queue_id, "req-partial")
        station.record_reclip_intent(
            request_id="req-partial",
            instance_id=1,
            anchor=ANCHOR,
            window_from=ANCHOR - 8,
            window_to=ANCHOR + 8,
            wall_offset=row.wall_offset,
            sources=(CAMERA, "camera-b"),
            required_from=row.required_from,
            required_to=row.required_to,
            identity=identity,
        )
        # 只落一路相机的已定稿产物: 窗口覆盖但冻结的第二路来源缺失。
        original_dir = self.original_dir()
        media = _media(self.row())[0]
        clip_name = cast(str, media["clip_file"])
        keyframe_name = cast(str, media["keyframe_file"])
        artifact = (
            self.evidence / f"{identity}-{round((ANCHOR - 8) * 1000)}-{round((ANCHOR + 8) * 1000)}"
        )
        artifact.mkdir()
        shutil.copy(original_dir / clip_name, artifact / clip_name)
        shutil.copy(original_dir / keyframe_name, artifact / keyframe_name)
        (artifact / "metadata.json").write_text(
            json.dumps(
                {
                    "evidence_id": identity,
                    "host_id": HOST,
                    "anchor": ANCHOR,
                    "window_from": ANCHOR - 8,
                    "window_to": ANCHOR + 8,
                    "wall_offset": row.wall_offset,
                    "covered_from": ANCHOR - 8,
                    "covered_to": ANCHOR + 8,
                    "media": [
                        {
                            "media_path": CAMERA,
                            "clip_file": clip_name,
                            "keyframe_file": keyframe_name,
                            "actual_from": ANCHOR - 8,
                            "actual_to": ANCHOR + 8,
                            "material_generation": "original",
                            "segment_generations": {"original": 1},
                            "sha256": media["sha256"],
                            "size_bytes": media["size_bytes"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        transport = FakeReclipTransport([claim("req-partial", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        self.assertEqual(len(transport.reports), 1)
        self.assertEqual(transport.reports[0][1].failure_code, "reclip_source_missing")
        self.assertEqual(self.center.posts, [])
        record = station.reclip_request("req-partial")
        assert record is not None
        self.assertEqual(record.terminal_error_code, "reclip_source_missing")
        self.assertIsNone(record.evidence_id)

    def test_missing_frozen_source_is_observable_and_not_registered(self) -> None:
        state = open_local_state(str(self.root / "unmapped.sqlite"))
        self.addCleanup(state.close)
        enqueue(state)
        transport = FakeReclipTransport([claim("req-3", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor(state)).run_once()
        )
        self.assertEqual(len(transport.reports), 1)
        self.assertEqual(transport.reports[0][1].failure_code, "reclip_source_missing")
        self.assertEqual(self.center.posts, [])
        # 来源映射缺失也要持久化可查询原因, 不伪造墙钟/相机映射, 也不静默丢弃待办。
        record = state.station(STATION).reclip_request("req-3")
        assert record is not None
        self.assertIn("reclip_source_missing", record.last_error or "")
        self.assertIsNone(record.wall_offset)
        self.assertEqual(record.sources, ())
        self.assertIsNone(record.identity)
        self.assertIsNone(record.media_results)

        # 重启后同一请求仍带原因; 重复领取既不登记也不重切, 仍回报同一终局原因。
        state.close()
        reopened = self.open_state("unmapped.sqlite")
        self.addCleanup(reopened.close)
        retry = FakeReclipTransport([claim("req-3", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=retry, executor=self.executor(reopened)).run_once()
        )
        self.assertEqual(len(retry.reports), 1)
        self.assertEqual(retry.reports[0][1].failure_code, "reclip_source_missing")
        self.assertEqual(self.center.posts, [])
        recovered = reopened.station(STATION).reclip_request("req-3")
        assert recovered is not None
        self.assertIn("reclip_source_missing", recovered.last_error or "")

    def test_missing_evidence_row_is_persisted_as_source_missing(self) -> None:
        # 本机没有该实例/锚点的冻结证据行: 仍要留下可查询失败原因, 不凭空造映射。
        state = self.open_state("row-missing.sqlite")
        self.addCleanup(state.close)
        enqueue(state)
        transport = FakeReclipTransport(
            [claim("req-4", start=ANCHOR - 8, end=ANCHOR + 8, instance_id=9)]
        )
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor(state)).run_once()
        )
        self.assertEqual(len(transport.reports), 1)
        self.assertEqual(transport.reports[0][1].failure_code, "reclip_source_missing")
        self.assertEqual(self.center.posts, [])
        record = state.station(STATION).reclip_request("req-4")
        assert record is not None
        self.assertIn("reclip_source_missing", record.last_error or "")
        self.assertIsNone(record.wall_offset)
        self.assertEqual(record.sources, ())
        self.assertIsNone(record.identity)

    def test_lost_sql_index_recovers_the_finalized_artifact_without_reslicing(self) -> None:
        # 进程在原子定稿后、写 SQL 前崩溃: 产物目录已在, 但本机索引 media_results 仍为 NULL。
        original_dirs = {directory for directory in self.evidence.iterdir() if directory.is_dir()}

        def crash_after_publish(_store: StationStore, _request_id: str, **_kwargs: object) -> None:
            raise RuntimeError("process loss after atomic publish")

        with patch.object(StationStore, "record_reclip_slice", crash_after_publish):
            transport = FakeReclipTransport([claim("req-2b", start=ANCHOR - 8, end=ANCHOR + 8)])
            with self.assertRaises(RuntimeError):
                ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        record = self.state.station(STATION).reclip_request("req-2b")
        assert record is not None
        self.assertIsNone(record.media_results)
        self.assertIsNotNone(record.identity)
        finalized = {
            directory for directory in self.evidence.iterdir() if directory.is_dir()
        } - original_dirs
        self.assertEqual(len(finalized), 1)

        # 重试前滚动素材消失; 恢复必须复用已定稿产物, 而不是重新查询分段。
        shutil.rmtree(self.recording)
        retry = FakeReclipTransport([claim("req-2b", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(ReclipCommandRunner(transport=retry, executor=self.executor()).run_once())
        self.assertEqual(len(retry.reports), 1)
        recovered = self.state.station(STATION).reclip_request("req-2b")
        assert recovered is not None
        self.assertEqual(recovered.evidence_id, retry.reports[0][1].evidence_id)
        self.assertIn(recovered.evidence_id, self.center.records)
        # 产物目录集合不变: 恢复不覆盖也不新建权威目录。
        self.assertEqual(
            {directory for directory in self.evidence.iterdir() if directory.is_dir()},
            original_dirs | finalized,
        )

    def test_tampered_or_missing_finalized_media_is_observable_without_reslicing(self) -> None:
        # 登记确认丢失后媒体仍在; 之后定稿媒体被篡改/删除, 登记准备失败必须折叠为可观察失败。
        self.center.drop_reply = True
        transport = FakeReclipTransport([claim("req-7", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=transport, executor=self.executor()).run_once()
        )
        self.assertEqual(transport.reports, [])
        record = self.state.station(STATION).reclip_request("req-7")
        assert record is not None
        assert record.media_results is not None
        assert record.identity is not None
        media = cast(list[dict[str, object]], json.loads(record.media_results))
        artifact = next(
            directory
            for directory in self.evidence.iterdir()
            if directory.is_dir() and directory.name.startswith(f"{record.identity}-")
        )
        clip = artifact / cast(str, media[0]["clip_file"])
        finalized = {directory for directory in self.evidence.iterdir() if directory.is_dir()}
        posts_after_first = list(self.center.posts)

        clip.write_bytes(clip.read_bytes() + b"tamper")
        tampered = FakeReclipTransport([claim("req-7", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(
            ReclipCommandRunner(transport=tampered, executor=self.executor()).run_once()
        )
        # 已定稿媒体损坏: 重试无法恢复, 终局失败回报中心结案 (S039)。
        self.assertEqual(len(tampered.reports), 1)
        self.assertEqual(tampered.reports[0][1].failure_code, "reclip_registration_failed")
        tampered_record = self.state.station(STATION).reclip_request("req-7")
        assert tampered_record is not None
        self.assertIn("ValueError", tampered_record.last_error or "")
        self.assertIsNone(tampered_record.evidence_id)

        clip.unlink()
        removed = FakeReclipTransport([claim("req-7", start=ANCHOR - 8, end=ANCHOR + 8)])
        self.assertTrue(ReclipCommandRunner(transport=removed, executor=self.executor()).run_once())
        self.assertEqual(len(removed.reports), 1)
        # 首次终局原因已冻结: 后续媒体删除只复用同一结案, 不改因也不重切 (S039/AC1)。
        self.assertEqual(removed.reports[0][1].failure_code, "reclip_registration_failed")
        self.assertEqual(removed.reports[0][1].detail, tampered.reports[0][1].detail)
        removed_record = self.state.station(STATION).reclip_request("req-7")
        assert removed_record is not None
        self.assertEqual(removed_record.terminal_error_code, "reclip_registration_failed")
        self.assertIn("ValueError", removed_record.last_error or "")
        self.assertIsNone(removed_record.evidence_id)
        # 不重切、不覆盖、不追加新引用。
        self.assertEqual(
            {directory for directory in self.evidence.iterdir() if directory.is_dir()}, finalized
        )
        self.assertEqual(self.center.posts, posts_after_first)


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
