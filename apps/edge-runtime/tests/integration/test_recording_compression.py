"""录像压缩候选集成: 真实 ffmpeg/ffprobe、真实留痕、失败保留原件与再切片路径 (S047)。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from time import time
from typing import cast
from unittest.mock import patch

from store_harness import ANCHOR, STATION, opening_state

from edge_runtime.evidence_media import EvidenceMediaConfiguration, EvidenceMediaWorker
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Instance, Lifecycle
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import EvidenceSource, LocalState, open_local_state
from edge_runtime.local_state.queues import PendingEvidence
from edge_runtime.recording_compression import (
    RecordingCompressionConfiguration,
    compress_aged_segments,
    validated_candidate,
)

FFMPEG, FFPROBE = Path("/usr/bin/ffmpeg"), Path("/usr/bin/ffprobe")
CAMERA = "camera-a"
FRESH_CAMERA = "camera-b"
RAW_WINDOW_SECONDS = 3600
SEGMENT_SECONDS = 2
CRF = 30
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


def segments(directory: Path, start: float, count: int, *, crf: int = 23) -> None:
    """真实 ffmpeg 生成 2s 分段, 并按本机墙钟命名成录像生产者的分段契约。"""
    directory.mkdir(parents=True, exist_ok=True)
    command = [str(FFMPEG), *_SEGMENTS, "-crf", str(crf), "-t", str(count * SEGMENT_SECONDS)]
    subprocess.run([*command, str(directory / "p%03d.mp4")], check=True)
    for index, part in enumerate(sorted(directory.glob("p*.mp4"))):
        stamp = datetime.fromtimestamp(start + index * SEGMENT_SECONDS).strftime(
            "%Y-%m-%d_%H-%M-%S-%f"
        )
        part.rename(directory / f"{stamp}.mp4")


def probe(path: Path) -> dict[str, object]:
    """测试侧独立用 ffprobe 测量, 不复用生产代码的解析结果自证。"""
    completed = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration:stream=codec_name,nb_read_frames",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
    )
    payload = cast(dict[str, object], json.loads(completed.stdout))
    stream = cast(list[dict[str, object]], payload["streams"])[0]
    fmt = cast(dict[str, object], payload["format"])
    return {
        "codec": stream["codec_name"],
        "frames": int(cast(str, stream["nb_read_frames"])),
        "duration": float(cast(str, fmt["duration"])),
    }


def decode(path: Path) -> None:
    """-xerror 让坏帧以非零退出而不是被容错吞掉。"""
    subprocess.run(
        [str(FFMPEG), "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        check=True,
    )


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RecordingCompressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.recording = self.root / "recordings"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configuration(
        self, *, ffmpeg: Path = FFMPEG, ffprobe: Path = FFPROBE
    ) -> RecordingCompressionConfiguration:
        return RecordingCompressionConfiguration(
            recording_directory=self.recording,
            ffmpeg_binary=ffmpeg,
            ffprobe_binary=ffprobe,
            compression_age_seconds=RAW_WINDOW_SECONDS,
            segment_duration_seconds=SEGMENT_SECONDS,
            crf=CRF,
            timeout_seconds=60.0,
            recording_timezone=None,
        )

    def test_aged_segments_get_validated_h264_candidate_and_inner_window_stays_original(
        self,
    ) -> None:
        aged = self.recording / CAMERA
        segments(aged, time() - 3 * RAW_WINDOW_SECONDS, 2)
        fresh = self.recording / FRESH_CAMERA
        segments(fresh, time() - 30, 1)
        source = sorted(aged.glob("*.mp4"))[0]
        original = digest(source)

        summary = compress_aged_segments(self.configuration())

        self.assertEqual((summary.created, summary.skipped, summary.failed), (2, 0, 0))
        self.assertEqual(list(fresh.glob("*.compressed.mp4")), [])
        self.assertEqual(digest(source), original)
        candidate = validated_candidate(source)
        assert candidate is not None
        candidate_path = source.with_suffix(".compressed.mp4")
        self.assertEqual(candidate.source_sha256, original)
        self.assertEqual(candidate.source_size_bytes, source.stat().st_size)
        self.assertEqual(candidate.candidate_sha256, digest(candidate_path))
        self.assertEqual(candidate.candidate_size_bytes, candidate_path.stat().st_size)
        self.assertEqual(candidate.crf, CRF)
        self.assertEqual(candidate.encoder, "libx264")
        self.assertEqual(candidate.preset, "veryfast")
        self.assertEqual(candidate.generation, "compressed")
        measured = probe(candidate_path)
        self.assertEqual(measured["codec"], "h264")  # 默认 H.264: 浏览器可直接回看
        self.assertEqual(measured["frames"], candidate.candidate_frames)
        self.assertLess(
            abs(cast(float, measured["duration"]) - candidate.source_duration_seconds), 0.5
        )
        decode(candidate_path)
        document = json.loads(source.with_suffix(".compressed.json").read_text(encoding="utf-8"))
        self.assertEqual(document["crf"], CRF)
        self.assertEqual(document["generation"], "compressed")
        self.assertEqual(document["candidate_sha256"], candidate.candidate_sha256)

    def test_encode_failure_keeps_original_and_records_alert(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)

        with self.assertLogs("edge_runtime", level="ERROR") as logs:
            summary = compress_aged_segments(self.configuration(ffmpeg=Path("/nonexistent/ffmpeg")))

        self.assertEqual((summary.created, summary.failed), (0, 1))
        self.assertTrue(any("edge.recording_compression.failed" in line for line in logs.output))
        self.assertTrue(any("encode_failed" in line for line in logs.output))
        self.assertEqual(digest(source), original)
        self.assertIsNone(validated_candidate(source))
        self.assertFalse(source.with_suffix(".compressed.mp4").exists())
        self.assertFalse(source.with_suffix(".compressed.json").exists())

    def test_measurement_mismatch_rejects_candidate_and_keeps_original(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)

        for field in ("nb_read_frames", "duration", "codec_name"):
            with self.subTest(field=field):
                with self.assertLogs("edge_runtime", level="ERROR") as logs:
                    summary = compress_aged_segments(
                        self.configuration(ffprobe=self._probe_double(field))
                    )
                self.assertEqual((summary.created, summary.failed), (0, 1))
                self.assertTrue(any("verification_failed" in line for line in logs.output))
                self.assertEqual(digest(source), original)
                self.assertIsNone(validated_candidate(source))
                self.assertFalse(source.with_suffix(".compressed.mp4").exists())

    def test_unverified_or_tampered_candidate_is_not_a_safe_replacement(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        candidate = source.with_suffix(".compressed.mp4")
        candidate.write_bytes(b"not a video")
        self.assertIsNone(validated_candidate(source))

        recovered = compress_aged_segments(self.configuration())

        self.assertEqual((recovered.created, recovered.failed), (1, 0))
        self.assertIsNotNone(validated_candidate(source))
        decode(candidate)

        # 同尺寸覆盖: 字节数不变而摘要已变, 未变化判定必须靠留痕记录的写入时刻发现它。
        length = candidate.stat().st_size
        candidate.write_bytes(b"\0" * length)
        self.assertIsNone(validated_candidate(source))
        overwritten = compress_aged_segments(self.configuration())
        self.assertEqual((overwritten.created, overwritten.failed), (1, 0))
        repaired_candidate = validated_candidate(source)
        assert repaired_candidate is not None
        self.assertEqual(digest(candidate), repaired_candidate.candidate_sha256)
        self.assertNotEqual(candidate.read_bytes(), b"\0" * length)

        # 留痕里的 CRF 越界同样不是可消费产物 (与配置侧共用同一条 0-51 规则)。
        provenance = source.with_suffix(".compressed.json")
        document = json.loads(provenance.read_text(encoding="utf-8"))
        document["crf"] = 99
        provenance.write_text(json.dumps(document), encoding="utf-8")
        self.assertIsNone(validated_candidate(source))
        rejected = compress_aged_segments(self.configuration())
        self.assertEqual((rejected.created, rejected.failed), (1, 0))
        self.assertIsNotNone(validated_candidate(source))

        # 写入时刻被清零(归档/清洗工具)是合法事实: 不能误判成非法留痕并每轮重编码。
        os.utime(candidate, ns=(0, 0))
        document = json.loads(provenance.read_text(encoding="utf-8"))
        document["candidate_mtime_ns"] = 0
        provenance.write_text(json.dumps(document), encoding="utf-8")
        self.assertIsNotNone(validated_candidate(source))
        self.assertEqual(compress_aged_segments(self.configuration()).skipped, 1)

    def test_unreadable_recording_directory_alerts_without_failing_the_pass(self) -> None:
        segments(self.recording / CAMERA, time() - 3 * RAW_WINDOW_SECONDS, 1)

        with (
            patch.object(Path, "iterdir", side_effect=PermissionError("denied")),
            self.assertLogs("edge_runtime", level="ERROR") as logs,
        ):
            summary = compress_aged_segments(self.configuration())

        # 目录级失败只告警并结束本轮, 不冒充逐分段失败计数, 也不结束判定运行时。
        self.assertEqual(0, summary.created)
        self.assertTrue(any("io_failed" in line for line in logs.output))

    def test_valid_candidate_is_reused_without_reencoding(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        first = compress_aged_segments(self.configuration())
        self.assertEqual((first.created, first.failed), (1, 0))
        candidate = source.with_suffix(".compressed.mp4")
        encoded = digest(candidate)
        original = digest(source)

        second = compress_aged_segments(self.configuration())

        self.assertEqual((second.created, second.skipped, second.failed), (0, 1, 0))
        self.assertEqual(digest(candidate), encoded)
        self.assertEqual(digest(source), original)

    def test_archived_or_unprovable_segments_are_not_compressed(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        sidecar = source.with_suffix(".material.json")
        sidecar.write_text(json.dumps({"generation": "compressed"}), encoding="utf-8")

        archived = compress_aged_segments(self.configuration())

        self.assertEqual((archived.created, archived.skipped, archived.failed), (0, 1, 0))
        self.assertFalse(source.with_suffix(".compressed.mp4").exists())
        sidecar.write_text("not json", encoding="utf-8")
        with self.assertLogs("edge_runtime", level="ERROR") as logs:
            unprovable = compress_aged_segments(self.configuration())
        self.assertEqual((unprovable.created, unprovable.failed), (0, 1))
        self.assertTrue(any("material_provenance_invalid" in line for line in logs.output))
        self.assertFalse(source.with_suffix(".compressed.mp4").exists())

    def test_compressed_candidate_serves_existing_reslice_path_as_compressed(self) -> None:
        camera = self.recording / CAMERA
        segments(camera, ANCHOR - 6, 7)
        summary = compress_aged_segments(self.configuration())
        self.assertEqual((summary.created, summary.failed), (7, 0))
        # 模拟 S048 的原子替换: 候选成为分段并声明 compressed; 本票自身不替换任何原件。
        originals = sorted(
            path for path in camera.glob("*.mp4") if not path.name.endswith(".compressed.mp4")
        )
        self.assertEqual(len(originals), 7)
        for segment in originals:
            os.replace(segment.with_suffix(".compressed.mp4"), segment)
            segment.with_suffix(".material.json").write_text(
                json.dumps({"generation": "compressed"}), encoding="utf-8"
            )
        state = self._state()
        self.addCleanup(state.close)
        self._enqueue(state)
        worker = EvidenceMediaWorker(
            state=state,
            host_id="host-01",
            recording_directory=self.recording,
            ffmpeg_binary=FFMPEG,
            configuration=EvidenceMediaConfiguration(self.root / "evidence", FFPROBE, 30.0, None),
            interval=1.0,
        )

        self.assertEqual(worker.drain(), 1)

        (pending,) = state.station(STATION).pending_evidence()
        self.assertIsNotNone(pending.sliced_at)
        (media,) = _media(pending)
        self.assertEqual(media["material_generation"], "compressed")
        self.assertEqual(set(cast(dict[str, int], media["segment_generations"])), {"compressed"})
        clip, keyframe = _files(self.root / "evidence", media)
        covered = cast(float, media["actual_to"]) - cast(float, media["actual_from"])
        self.assertLess(abs(_duration(clip) - covered), 0.5)
        decode(clip)
        decode(keyframe)

    def _state(self) -> LocalState:
        return open_local_state(
            str(self.root / "state.sqlite"),
            evidence_source=lambda station: (
                EvidenceSource(wall_offset=0.0, media_paths=(CAMERA,))
                if station == STATION
                else None
            ),
        )

    def _enqueue(self, state: LocalState) -> None:
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

    def _probe_double(self, field: str) -> Path:
        """在真实 ffprobe 之上伪造候选的单项测量, 证明容差校验真的会拒绝不一致产物。"""
        script = self.root / f"ffprobe-{field}"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, subprocess, sys\n"
            f"FIELD = {field!r}\n"
            "completed = subprocess.run(\n"
            '    ["/usr/bin/ffprobe", *sys.argv[1:]], capture_output=True, check=True\n'
            ")\n"
            "payload = json.loads(completed.stdout)\n"
            'if sys.argv[-1].endswith(".candidate.mp4"):\n'
            '    if FIELD == "nb_read_frames":\n'
            '        payload["streams"][0]["nb_read_frames"] = "1"\n'
            '    elif FIELD == "codec_name":\n'
            '        payload["streams"][0]["codec_name"] = "hevc"\n'
            "    else:\n"
            '        duration = float(payload["format"]["duration"])\n'
            '        payload["format"]["duration"] = str(duration + 60)\n'
            "sys.stdout.write(json.dumps(payload))\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script


def _media(pending: PendingEvidence) -> list[dict[str, object]]:
    assert pending.media_results is not None
    return cast(list[dict[str, object]], json.loads(pending.media_results))


def _files(evidence: Path, item: dict[str, object]) -> tuple[Path, Path]:
    clip_name = cast(str, item["clip_file"])
    directory = next(d for d in evidence.iterdir() if (d / clip_name).is_file())
    return directory / clip_name, directory / cast(str, item["keyframe_file"])


def _duration(path: Path) -> float:
    completed = subprocess.run(
        [str(FFPROBE), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        check=True,
    )
    return float(json.loads(completed.stdout)["format"]["duration"])


if __name__ == "__main__":
    unittest.main()
