"""证据切片集成: 真实 ffmpeg/ffprobe、真实 SQLite、冻结映射与失败待办 (S033)。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import cast

from store_harness import ANCHOR, STATION, opening_state

from edge_runtime.evidence_media import EvidenceMediaConfiguration, EvidenceMediaWorker
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import (
    Decision,
    EvidenceSpan,
    HostInstant,
    Instance,
    Lifecycle,
)
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import EvidenceSource, LocalState, open_local_state
from edge_runtime.local_state.queues import PendingEvidence

FFMPEG, FFPROBE = Path("/usr/bin/ffmpeg"), Path("/usr/bin/ffprobe")
CAMERAS = ("camera-a", "camera-b")
_FFMPEG_SEGMENTS = (
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
)


def segments(directory: Path, start: float, count: int, *, gap_after: int | None = None) -> None:
    """真实 ffmpeg 生成 2s 分段并按本机墙钟重命名; ``gap_after`` 后错开 0.4s 造真实缺口。"""
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(FFMPEG), *_FFMPEG_SEGMENTS, "-t", str(count * 2), str(directory / "p%03d.mp4")],
        check=True,
    )
    for index, part in enumerate(sorted(directory.glob("p*.mp4"))):
        shift = 0.4 if gap_after is not None and index > gap_after else 0.0
        stamp = datetime.fromtimestamp(start + index * 2 + shift).strftime("%Y-%m-%d_%H-%M-%S-%f")
        part.rename(directory / f"{stamp}.mp4")


def enqueue(
    state: LocalState,
    *,
    anchor: float = ANCHOR,
    start: float | None = None,
    end: float | None = None,
) -> None:
    store = state.station(STATION)
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
    store.commit(
        state=opening_state(),
        decisions=(decision,),
        evidence=(
            EvidenceClip(
                1,
                at,
                HostInstant(anchor - 5 if start is None else start),
                HostInstant(anchor + 5 if end is None else end),
            ),
        ),
        closed_instances=(instance,),
        report_provenance={},
    )


class EvidenceMediaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.recording = self.root / "recordings"
        self.evidence = self.root / "evidence"

    def state(self, *, offset: float = 0.0, paths: tuple[str, ...] = CAMERAS) -> LocalState:
        return open_local_state(
            str(self.root / "state.sqlite"),
            evidence_source=lambda station: (
                EvidenceSource(wall_offset=offset, media_paths=paths)
                if station == STATION
                else None
            ),
        )

    def worker(
        self, state: LocalState, *, evidence: Path | None = None, ffmpeg: Path = FFMPEG
    ) -> EvidenceMediaWorker:
        configuration = EvidenceMediaConfiguration(
            evidence_directory=evidence or self.evidence,
            ffprobe_binary=FFPROBE,
            slice_timeout_seconds=30.0,
            recording_timezone=None,
        )
        return EvidenceMediaWorker(
            state=state,
            host_id="host-01",
            recording_directory=self.recording,
            ffmpeg_binary=ffmpeg,
            configuration=configuration,
            interval=1.0,
        )

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
            actual_from, actual_to = (
                cast(float, item["actual_from"]),
                cast(float, item["actual_to"]),
            )
            self.assertLessEqual(actual_from, ANCHOR)
            self.assertGreaterEqual(actual_to, ANCHOR)
            self.assertLess(abs(_duration(clip) - (actual_to - actual_from)), 0.5)
            self.assertEqual(_codec(clip), "video")
            self.assertEqual(_codec(keyframe), "video")
            self.assertEqual(item["sha256"], hashlib.sha256(clip.read_bytes()).hexdigest())
            self.assertEqual(item["size_bytes"], clip.stat().st_size)
            self.assertEqual(item["material_generation"], "original")
        self.assertEqual(self.worker(reopened).drain(), 0)  # 已覆盖, 不再重切

    def test_a_real_recording_gap_is_material_missing(self) -> None:
        for camera in CAMERAS:
            segments(self.recording / camera, ANCHOR - 6, 8, gap_after=2)
        state = self.state()
        enqueue(state)
        self.assertEqual(self.worker(state).drain(), 0)
        (pending,) = state.station(STATION).pending_evidence()
        self.assertIsNone(pending.sliced_at)
        self.assertIn("material_missing", cast(str, pending.last_error))

    def test_missing_material_and_missing_mapping_stay_pending(self) -> None:
        state = self.state()
        enqueue(state)
        self.assertEqual(self.worker(state).drain(), 0)
        (pending,) = state.station(STATION).pending_evidence()
        self.assertIn("material_missing", cast(str, pending.last_error))
        unmapped = open_local_state(str(self.root / "unmapped.sqlite"))
        self.addCleanup(unmapped.close)
        enqueue(unmapped)
        self.assertEqual(self.worker(unmapped).drain(), 0)
        (legacy,) = unmapped.station(STATION).pending_evidence()
        self.assertIn("mapping_missing", cast(str, legacy.last_error))

    def test_media_io_failure_is_durable_and_does_not_block_other_pending(self) -> None:
        segments(self.recording / CAMERAS[0], ANCHOR - 6, 8)
        state = self.state(paths=(CAMERAS[0],))
        enqueue(state)
        self.assertEqual(self.worker(state, ffmpeg=Path("/nonexistent/ffmpeg")).drain(), 0)
        (pending,) = state.station(STATION).pending_evidence()
        self.assertIn("ffmpeg_failed", cast(str, pending.last_error))
        self.assertEqual(self.worker(state, evidence=Path("/dev/null/s033")).drain(), 0)
        (io_failed,) = state.station(STATION).pending_evidence()
        self.assertIn("media_io_failed", cast(str, io_failed.last_error))
        # 同一轮里无素材待办失败不得阻塞有素材待办成功
        mixed = self.state(paths=(CAMERAS[0],))
        enqueue(mixed)
        enqueue(mixed, anchor=ANCHOR + 1000)
        self.assertEqual(self.worker(mixed).drain(), 1)
        remaining = mixed.station(STATION).evidence_awaiting_slice()
        self.assertEqual(len(remaining), 1)
        self.assertIn("material_missing", cast(str, remaining[0].last_error))

    def test_existing_artifact_metadata_is_reused_and_widening_preserves_it(self) -> None:
        segments(self.recording / CAMERAS[0], ANCHOR - 6, 8)
        state = self.state(paths=(CAMERAS[0],))
        enqueue(state)
        self.assertEqual(self.worker(state).drain(), 1)
        (first,) = state.station(STATION).pending_evidence()
        (first_media,) = _media(first)
        first_dir, _ = _files(self.evidence, first_media)
        first_dir = first_dir.parent
        # 模拟 rename 后 SQLite 前崩溃: 清空索引并把旧产物 metadata 改成可辨识哨兵。
        state.station(STATION)._connection.execute(
            "UPDATE local_evidence_queue SET sliced_at=NULL, media_results=NULL,"
            " covered_from=NULL, covered_to=NULL"
        )
        sentinel = "f" * 64
        metadata = json.loads((first_dir / "metadata.json").read_text(encoding="utf-8"))
        metadata["media"][0]["sha256"] = sentinel
        (first_dir / "metadata.json").write_text(
            json.dumps(metadata, separators=(",", ":")), encoding="utf-8"
        )
        self.assertEqual(self.worker(state).drain(), 1)
        (reused,) = state.station(STATION).pending_evidence()
        self.assertEqual(_media(reused)[0]["sha256"], sentinel)  # 复用旧权威, 不伪报新 hash
        # 窗口后来扩大: 旧 artifact 保留, 旧较小片段不被当作新窗口完成。
        enqueue(state, start=ANCHOR - 65)
        self.assertEqual(len(state.station(STATION).evidence_awaiting_slice()), 1)
        self.assertEqual(self.worker(state).drain(), 0)
        self.assertTrue(first_dir.is_dir())
        self.assertEqual(
            _media(state.station(STATION).pending_evidence()[0])[0]["sha256"], sentinel
        )


def _media(pending: PendingEvidence) -> list[dict[str, object]]:
    assert pending.media_results is not None
    return cast(list[dict[str, object]], json.loads(pending.media_results))


def _files(evidence: Path, item: dict[str, object]) -> tuple[Path, Path]:
    clip_name = cast(str, item["clip_file"])
    directory = next(d for d in evidence.iterdir() if (d / clip_name).is_file())
    return directory / clip_name, directory / cast(str, item["keyframe_file"])


def _probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    return cast(dict[str, object], json.loads(completed.stdout))


def _duration(path: Path) -> float:
    return float(cast(str, cast(dict[str, object], _probe(path)["format"])["duration"]))


def _codec(path: Path) -> object:
    return cast(list[dict[str, object]], _probe(path)["streams"])[0]["codec_type"]


if __name__ == "__main__":
    unittest.main()
