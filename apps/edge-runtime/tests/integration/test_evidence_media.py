"""证据切片集成: 真实 ffmpeg/ffprobe、真实 SQLite 重启、冻结映射与失败待办 (S033)。"""

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
CAMERA = "camera-abc"


def segments(directory: Path, start: float, count: int, duration: int = 2) -> None:
    """用真实 ffmpeg 生成连续分段, 再按本机墙钟重命名, 模拟 MediaMTX recordPath 形状。"""
    directory.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x120:rate=10",
            "-t",
            str(count * duration),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "10",
            "-f",
            "segment",
            "-segment_time",
            str(duration),
            "-reset_timestamps",
            "1",
            str(directory / "p%03d.mp4"),
        ],
        check=True,
    )
    for index, part in enumerate(sorted(directory.glob("p*.mp4"))):
        stamp = datetime.fromtimestamp(start + index * duration).strftime("%Y-%m-%d_%H-%M-%S-%f")
        part.rename(directory / f"{stamp}.mp4")


def enqueue(state: LocalState, *, start: float = ANCHOR - 5, end: float = ANCHOR + 5) -> None:
    store = state.station(STATION)
    anchor = HostInstant(ANCHOR)
    decision = Decision(
        instance_id=1,
        verdict=Verdict.PASS,
        reasons=(),
        violations=(),
        lifecycle=Lifecycle.CLOSED_BY_COMPLETE_SET,
        evidence=EvidenceSpan.at(anchor),
    )
    instance = Instance(instance_id=1, opened_at=anchor, last_observation_at=anchor)
    store.commit(
        state=opening_state(),
        decisions=(decision,),
        evidence=(EvidenceClip(1, anchor, HostInstant(start), HostInstant(end)),),
        closed_instances=(instance,),
        report_provenance={},
    )


class EvidenceMediaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.recording = self.root / "recordings"
        self.evidence = self.root / "evidence"

    def state(self, offset: float = 0.0) -> LocalState:
        return open_local_state(
            str(self.root / "state.sqlite"),
            evidence_source=lambda station: (
                EvidenceSource(wall_offset=offset, media_paths=(CAMERA,))
                if station == STATION
                else None
            ),
        )

    def worker(
        self, state: LocalState, *, ffmpeg: Path = FFMPEG, ffprobe: Path = FFPROBE
    ) -> EvidenceMediaWorker:
        configuration = EvidenceMediaConfiguration(
            evidence_directory=self.evidence,
            ffprobe_binary=ffprobe,
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

    def test_success_persists_decodable_clip_and_survives_sqlite_restart(self) -> None:
        # 入队时冻结 wall_offset=100; 重启后当前偏移变 0, 旧待办必须仍按冻结映射切到正确录像。
        segments(self.recording / CAMERA, ANCHOR - 6 + 100, 8)
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
        (media,) = _media(pending)
        directory = _artifact_dir(self.evidence, media)
        clip = directory / cast(str, media["clip_file"])
        self.assertEqual(media["material_generation"], "original")
        self.assertEqual(media["sha256"], hashlib.sha256(clip.read_bytes()).hexdigest())
        self.assertEqual(media["size_bytes"], clip.stat().st_size)
        self.assertTrue((directory / cast(str, media["keyframe_file"])).is_file())
        streams = cast(list[dict[str, object]], _probe(clip)["streams"])
        self.assertEqual(streams[0]["codec_type"], "video")
        self.assertEqual(self.worker(reopened).drain(), 0)  # 已覆盖, 不再重切

    def test_missing_material_and_missing_mapping_stay_pending_with_reason(self) -> None:
        state = self.state()
        enqueue(state)
        self.assertEqual(self.worker(state).drain(), 0)
        (pending,) = state.station(STATION).pending_evidence()
        self.assertIsNone(pending.sliced_at)
        self.assertIn("material_missing", cast(str, pending.last_error))
        unmapped = open_local_state(str(self.root / "unmapped.sqlite"))
        self.addCleanup(unmapped.close)
        enqueue(unmapped)
        self.assertEqual(self.worker(unmapped).drain(), 0)
        (legacy,) = unmapped.station(STATION).pending_evidence()
        self.assertIn("mapping_missing", cast(str, legacy.last_error))

    def test_ffmpeg_and_finalize_failures_keep_pending(self) -> None:
        segments(self.recording / CAMERA, ANCHOR - 6, 8)
        state = self.state()
        enqueue(state)
        self.assertEqual(self.worker(state, ffmpeg=Path("/nonexistent/ffmpeg")).drain(), 0)
        (pending,) = state.station(STATION).pending_evidence()
        self.assertIn("ffmpeg_failed", cast(str, pending.last_error))
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.evidence.chmod(0o500)
        self.addCleanup(self.evidence.chmod, 0o700)
        self.assertEqual(self.worker(state).drain(), 0)
        (still,) = state.station(STATION).pending_evidence()
        self.assertIn("finalize_failed", cast(str, still.last_error))

    def test_a_widened_window_re_slices_without_overwriting_the_old_artifact(self) -> None:
        segments(self.recording / CAMERA, ANCHOR - 6, 8)
        state = self.state()
        enqueue(state)
        self.assertEqual(self.worker(state).drain(), 1)
        (first,) = state.station(STATION).pending_evidence()
        (first_media,) = _media(first)
        first_dir = _artifact_dir(self.evidence, first_media)
        enqueue(state, start=ANCHOR - 65)
        self.assertEqual(len(state.station(STATION).evidence_awaiting_slice()), 1)
        self.assertEqual(self.worker(state).drain(), 0)  # 更宽窗口素材缺口, 旧结果保留
        (widened,) = state.station(STATION).pending_evidence()
        self.assertEqual(widened.media_results, first.media_results)
        self.assertTrue(first_dir.is_dir())
        self.assertIn("material_missing", cast(str, widened.last_error))


def _media(pending: PendingEvidence) -> list[dict[str, object]]:
    assert pending.media_results is not None
    return cast(list[dict[str, object]], json.loads(pending.media_results))


def _artifact_dir(evidence: Path, media: dict[str, object]) -> Path:
    clip_file = cast(str, media["clip_file"])
    return next(directory for directory in evidence.iterdir() if (directory / clip_file).is_file())


def _probe(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
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


if __name__ == "__main__":
    unittest.main()
