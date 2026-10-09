"""证据切片集成: 真实 ffmpeg/ffprobe、真实 SQLite、冻结映射与失败待办 (S033)。"""

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

from store_harness import ANCHOR, STATION, opening_state

from edge_runtime.evidence_media import (
    EvidenceMediaConfiguration,
    EvidenceMediaWorker,
    slice_evidence,
)
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Instance, Lifecycle
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import EvidenceSource, LocalState, open_local_state
from edge_runtime.local_state.queues import PendingEvidence

FFMPEG, FFPROBE = Path("/usr/bin/ffmpeg"), Path("/usr/bin/ffprobe")
CAMERAS = ("camera-a", "camera-b")
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


def segments(
    directory: Path,
    start: float,
    count: int,
    *,
    gap_after: int | None = None,
    overlap_after: int | None = None,
    crf: int = 23,
) -> None:
    """真实 ffmpeg 生成 2s 分段并按本机墙钟重命名; gap/overlap 在指定段后错开造真实边界。"""
    directory.mkdir(parents=True, exist_ok=True)
    command = [str(FFMPEG), *_SEGMENTS, "-crf", str(crf), "-t", str(count * 2)]
    subprocess.run([*command, str(directory / "p%03d.mp4")], check=True)
    shift = 0.0
    for index, part in enumerate(sorted(directory.glob("p*.mp4"))):
        if gap_after is not None and index > gap_after:
            shift = 0.4
        if overlap_after is not None and index > overlap_after:
            shift = -0.5
        stamp = datetime.fromtimestamp(start + index * 2 + shift).strftime("%Y-%m-%d_%H-%M-%S-%f")
        part.rename(directory / f"{stamp}.mp4")


def enqueue(state: LocalState, *, anchor: float = ANCHOR, start: float | None = None) -> None:
    at = HostInstant(anchor)
    decision = Decision(
        instance_id=1,
        verdict=Verdict.PASS,
        reasons=(),
        violations=(),
        lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
        evidence=EvidenceSpan.at(at),
    )
    instance = Instance(instance_id=1, opened_at=at, last_observation_at=at)
    start_at = HostInstant(anchor - 5 if start is None else start)
    clip = EvidenceClip(1, at, start_at, HostInstant(anchor + 5))
    state.station(STATION).commit(
        state=opening_state(),
        decisions=(decision,),
        evidence=(clip,),
        closed_instances=(instance,),
        report_provenance={},
        latched_at="2026-10-09T00:00:00+00:00",
        latched_monotonic=HostInstant(1.0),
    )


class EvidenceMediaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.recording = self.root / "recordings"
        self.evidence = self.root / "evidence"

    def state(
        self, name: str = "state.sqlite", *, offset: float = 0.0, paths: tuple[str, ...] = CAMERAS
    ) -> LocalState:
        return open_local_state(
            str(self.root / name),
            evidence_source=lambda station: (
                EvidenceSource(wall_offset=offset, media_paths=paths)
                if station == STATION
                else None
            ),
        )

    def config(self, evidence: Path | None = None) -> EvidenceMediaConfiguration:
        return EvidenceMediaConfiguration(evidence or self.evidence, FFPROBE, 30.0, None)

    def worker(
        self, state: LocalState, *, evidence: Path | None = None, ffmpeg: Path = FFMPEG
    ) -> EvidenceMediaWorker:
        return EvidenceMediaWorker(
            state=state,
            host_id="host-01",
            recording_directory=self.recording,
            ffmpeg_binary=ffmpeg,
            configuration=self.config(evidence),
            interval=1.0,
        )

    def publish(self, pending: PendingEvidence) -> tuple[dict[str, object], Path]:
        """公有 slice_evidence 先原子落盘, 模拟 rename 成功后 SQLite 前进程退出。"""
        clip_set = slice_evidence(
            pending,
            host_id="host-01",
            recording_directory=self.recording,
            ffmpeg_binary=FFMPEG,
            configuration=self.config(),
        )
        (media,) = cast(list[dict[str, object]], json.loads(clip_set.media_json))
        name = cast(str, media["clip_file"])
        artifact = next(d for d in self.evidence.iterdir() if (d / name).is_file())
        return media, artifact

    def test_multi_camera_clips_decode_and_survive_restart_with_frozen_mapping(self) -> None:
        # 入队冻结 wall_offset=100; 重启后当前偏移变 0, 旧待办必须仍按冻结映射切到正确录像。
        for camera in CAMERAS:
            segments(self.recording / camera, ANCHOR - 6 + 100, 8)
        state = self.state(offset=100.0)
        enqueue(state)
        state.close()
        reopened = self.state(offset=0.0)
        self.addCleanup(reopened.close)
        self.assertEqual(self.worker(reopened).drain(), 1)
        (pending,) = reopened.station(STATION).pending_evidence()
        self.assertIsNotNone(pending.sliced_at)
        self.assertLessEqual(cast(float, pending.covered_from), ANCHOR - 5)
        self.assertGreaterEqual(cast(float, pending.covered_to), ANCHOR + 5)
        media = _media(pending)
        self.assertEqual({cast(str, item["media_path"]) for item in media}, set(CAMERAS))
        for item in media:
            clip, keyframe = _files(self.evidence, item)
            actual_from = cast(float, item["actual_from"])
            actual_to = cast(float, item["actual_to"])
            self.assertLessEqual(actual_from, ANCHOR)
            self.assertGreaterEqual(actual_to, ANCHOR)
            self.assertLess(abs(_duration(clip) - (actual_to - actual_from)), 0.5)
            self.assertEqual(item["sha256"], hashlib.sha256(clip.read_bytes()).hexdigest())
            self.assertEqual(item["size_bytes"], clip.stat().st_size)
            self.assertEqual(item["material_generation"], "original")
            # 片段与关键帧必须真实解码; 关键帧必须落在 concat 真实来源 wall timeline 的 anchor 处。
            _decode(clip)
            _decode(keyframe)
            # fixture 分段从 ANCHOR+94 起, anchor wall=ANCHOR+100 正是第 4 段起点: 直接取
            # 原始分段帧作 expected, 不用 production 相同的 clip+offset 表达式自证。
            fixture = sorted((self.recording / cast(str, item["media_path"])).glob("*.mp4"))[3]
            self.assertEqual(_frame(fixture, 0.0, self.root / "a.jpg"), keyframe.read_bytes())
            other = _frame(fixture, 1.0, self.root / "b.jpg")
            self.assertNotEqual(other, keyframe.read_bytes())
        self.assertEqual(self.worker(reopened).drain(), 0)  # 已覆盖, 不再重切

    def test_material_and_mapping_failures_stay_pending(self) -> None:
        for camera in CAMERAS:
            segments(self.recording / camera, ANCHOR - 6, 8, gap_after=2)
        state = self.state()
        enqueue(state)
        self.assertEqual(self.worker(state).drain(), 0)
        (gapped,) = state.station(STATION).pending_evidence()
        self.assertIsNone(gapped.sliced_at)
        self.assertIn("material_missing", cast(str, gapped.last_error))
        unmapped = open_local_state(str(self.root / "unmapped.sqlite"))
        self.addCleanup(unmapped.close)
        enqueue(unmapped)
        self.assertEqual(self.worker(unmapped).drain(), 0)
        legacy = cast(str, unmapped.station(STATION).pending_evidence()[0].last_error)
        self.assertIn("mapping_missing", legacy)

    def test_overlapping_boundary_fails_unless_window_drops_old_segment(self) -> None:
        # 真实 MediaMTX 首段可与次段重叠 ~0.5s; concat 会重复内容并错位锚点, 不能静默成功。
        segments(self.recording / CAMERAS[0], ANCHOR - 6, 8, overlap_after=0)
        state = self.state(paths=(CAMERAS[0],))
        enqueue(state)  # 窗口从旧分段独有的部分开始, 旧分段必需
        self.assertEqual(self.worker(state).drain(), 0)
        (pending,) = state.station(STATION).pending_evidence()
        self.assertIsNone(pending.sliced_at)
        self.assertIn("material_overlap", cast(str, pending.last_error))
        state.close()
        # 窗口从重叠后起点开始, 旧分段不再必需: 选不受影响的连续 suffix 并成功。
        suffix = self.state("suffix.sqlite", paths=(CAMERAS[0],))
        self.addCleanup(suffix.close)
        enqueue(suffix, start=ANCHOR - 4.4)
        self.assertEqual(self.worker(suffix).drain(), 1)
        self.assertIsNotNone(suffix.station(STATION).pending_evidence()[0].sliced_at)

    def test_ffmpeg_and_finalize_failures_stay_pending_and_recover(self) -> None:
        segments(self.recording / CAMERAS[0], ANCHOR - 6, 8)
        state = self.state(paths=(CAMERAS[0],))
        enqueue(state)
        self.assertEqual(self.worker(state, ffmpeg=Path("/nonexistent/ffmpeg")).drain(), 0)
        (pending,) = state.station(STATION).pending_evidence()
        self.assertIn("ffmpeg_failed", cast(str, pending.last_error))
        _, artifact = self.publish(pending)
        shutil.rmtree(artifact)  # 换成同名普通文件, 下一次 rename 在真实 OS 上失败
        artifact.write_text("not a directory")
        self.assertEqual(self.worker(state).drain(), 0)
        (failed,) = state.station(STATION).pending_evidence()
        self.assertIsNone(failed.sliced_at)
        self.assertIn("finalize_failed", cast(str, failed.last_error))
        state.close()
        reopened = self.state(paths=(CAMERAS[0],))
        self.addCleanup(reopened.close)
        retained = cast(str, reopened.station(STATION).pending_evidence()[0].last_error)
        self.assertIn("finalize_failed", retained)
        artifact.unlink()  # 排除真实 OS 冲突后, 同一待办恢复成功
        self.assertEqual(self.worker(reopened).drain(), 1)
        self.assertIsNotNone(reopened.station(STATION).pending_evidence()[0].sliced_at)

    def test_existing_artifact_is_reused_and_widening_preserves_it(self) -> None:
        segments(self.recording / CAMERAS[0], ANCHOR - 6, 8)
        state = self.state(paths=(CAMERAS[0],))
        enqueue(state)
        (pending,) = state.station(STATION).pending_evidence()
        first, artifact = self.publish(pending)  # 产物已在盘上, 索引仍待办
        camera = self.recording / CAMERAS[0]
        old_segment = hashlib.sha256(next(camera.glob("*.mp4")).read_bytes()).hexdigest()
        shutil.rmtree(camera)
        segments(camera, ANCHOR - 6, 8, crf=18)  # 同覆盖, 不同真实 bytes
        new_segment = hashlib.sha256(next(camera.glob("*.mp4")).read_bytes()).hexdigest()
        self.assertNotEqual(old_segment, new_segment)
        state.close()
        reopened = self.state(paths=(CAMERAS[0],))
        self.addCleanup(reopened.close)
        self.assertEqual(self.worker(reopened).drain(), 1)
        (reused,) = reopened.station(STATION).pending_evidence()
        self.assertEqual(_media(reused)[0]["sha256"], first["sha256"])
        self.assertEqual(_media(reused)[0]["size_bytes"], first["size_bytes"])
        artifact_clip = artifact / cast(str, first["clip_file"])
        self.assertEqual(hashlib.sha256(artifact_clip.read_bytes()).hexdigest(), first["sha256"])
        # 窗口后来扩大: 旧 artifact 保留, 旧较小片段不被当作新窗口完成。
        enqueue(reopened, start=ANCHOR - 65)
        self.assertEqual(len(reopened.station(STATION).evidence_awaiting_slice()), 1)
        self.assertEqual(self.worker(reopened).drain(), 0)
        self.assertTrue(artifact.is_dir())
        final = _media(reopened.station(STATION).pending_evidence()[0])[0]
        self.assertEqual(final["sha256"], first["sha256"])


def _media(pending: PendingEvidence) -> list[dict[str, object]]:
    assert pending.media_results is not None
    return cast(list[dict[str, object]], json.loads(pending.media_results))


def _files(evidence: Path, item: dict[str, object]) -> tuple[Path, Path]:
    clip_name = cast(str, item["clip_file"])
    directory = next(d for d in evidence.iterdir() if (d / clip_name).is_file())
    return directory / clip_name, directory / cast(str, item["keyframe_file"])


def _decode(path: Path) -> None:
    """真实解码片段或关键帧; -xerror 使坏帧以非零退出而不是被容错吞掉。"""
    command = [str(FFMPEG), "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"]
    subprocess.run(command, check=True)


def _frame(clip: Path, offset: float, out: Path) -> bytes:
    """在片段 timeline 的指定偏移取一帧, 用同参数编码为 JPEG 返回 bytes。"""
    command = [str(FFMPEG), "-v", "error", "-ss", f"{offset:.3f}", "-i", str(clip)]
    subprocess.run([*command, "-frames:v", "1", "-q:v", "2", "-y", str(out)], check=True)
    return out.read_bytes()


def _duration(path: Path) -> float:
    command = [str(FFPROBE), "-v", "error", "-show_entries", "format=duration", "-of", "json"]
    completed = subprocess.run([*command, str(path)], capture_output=True, check=True)
    return float(json.loads(completed.stdout)["format"]["duration"])


if __name__ == "__main__":
    unittest.main()
