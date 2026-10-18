"""录像压缩集成: 真实编码、原子替换、进程退出恢复与再切片占用保护。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep, time
from typing import cast
from unittest.mock import patch

from store_harness import ANCHOR, STATION, opening_state

from edge_runtime.evidence_media import (
    EvidenceMediaConfiguration,
    EvidenceMediaError,
    EvidenceMediaWorker,
    recording_material_access,
    recording_segment_generation,
    slice_evidence,
)
from edge_runtime.judgment.evidence import EvidenceClip
from edge_runtime.judgment.model import Decision, EvidenceSpan, HostInstant, Instance, Lifecycle
from edge_runtime.judgment.reasons import Verdict
from edge_runtime.local_state import EvidenceSource, LocalState, QueueStatus, open_local_state
from edge_runtime.local_state.queues import PendingEvidence
from edge_runtime.recording_compression import (
    CompressionSummary,
    JudgmentBacklogChecker,
    RecordingCompressionConfiguration,
    RecordingCompressionError,
    RecordingCompressionWorker,
    compress_aged_segments,
    replace_validated_candidate,
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

    def wait_state(self, worker: RecordingCompressionWorker, state: str) -> None:
        deadline = monotonic() + 10
        while worker.status().state != state and monotonic() < deadline:
            sleep(0.01)
        self.assertEqual(worker.status().state, state)

    def test_readrate_limits_uninterrupted_archive_ingestion(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        worker = RecordingCompressionWorker(
            configuration=replace(self.configuration(), readrate=0.25), interval=1.0
        )
        started = monotonic()
        result = worker.drain()
        # FFmpeg 按墙钟限速; 用无暂停编码验证速率, 避免暂停后的追赶影响计时。
        # 下界给初始突发与帧重排留余量, 仍能识别未启用限速的真实编码。
        self.assertGreater(monotonic() - started, 1.5)
        self.assertEqual((result.replaced, result.failed), (1, 0))
        self.assertEqual(recording_segment_generation(source), "compressed")

    def test_pause_confirms_real_process_stop_and_freezes_timeout_until_resume(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)
        worker = RecordingCompressionWorker(
            configuration=replace(self.configuration(), readrate=0.5, timeout_seconds=4.0),
            interval=1.0,
        )
        results = []
        thread = Thread(target=lambda: results.append(worker.drain()))
        thread.start()
        try:
            self.wait_state(worker, "running")
            pid = worker.status().pid
            assert pid is not None
            worker.pause()
            worker.pause()
            self.wait_state(worker, "paused")
            self.assertIn("T (stopped)", Path(f"/proc/{pid}/status").read_text())
            self.assertEqual(digest(source), original)
            self.assertIsNone(validated_candidate(source))
            # 同目录的第二个任务不能越过暂停占用, 重复 drain 也不产生第二个子进程。
            self.assertEqual(worker.drain().created, 0)
            other = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
            self.assertEqual(other.drain().created, 0)
            sleep(4.2)  # 超过命令超时, 但暂停不消耗实际执行预算。
            worker.resume()
            worker.resume()
            thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertEqual((results[0].replaced, results[0].failed), (1, 0))
            self.assertEqual(recording_segment_generation(source), "compressed")
            self.assertEqual(worker.status().state, "idle")
        finally:
            worker.abort()
            thread.join(5)

    def test_judgment_backlog_checker_validates_and_tracks_hysteresis(self) -> None:
        depths = {"observation": 0, "report": 0, "evidence": 0, "health": 0}

        def provider() -> tuple[QueueStatus, ...]:
            return tuple(
                QueueStatus(
                    name=name,
                    depth=depth,
                    oldest_queue_id=1,
                    last_failure=None,
                    last_failed_queue_id=None,
                    limit=1000,
                )
                for name, depth in depths.items()
            )

        checker = JudgmentBacklogChecker(provider, threshold=10, recovery_threshold=4)

        # 初始深度为 0: 无积压
        backlogged, reason = checker()
        self.assertFalse(backlogged)
        self.assertIsNone(reason)

        # 深度上升但未达阈值 (9 < 10)
        depths["observation"] = 9
        backlogged, reason = checker()
        self.assertFalse(backlogged)
        self.assertIsNone(reason)

        # 达到阈值 10: 触发让路
        depths["observation"] = 10
        backlogged, reason = checker()
        self.assertTrue(backlogged)
        self.assertIsNotNone(reason)
        self.assertIn("judgment backlog in observation", reason or "")
        self.assertIn("depth 10 >= threshold 10", reason or "")

        # 深度轻微回落但高于恢复阈值 (8 > 4): 保持滞后让路状态, 避免抖动
        depths["observation"] = 8
        backlogged, reason = checker()
        self.assertTrue(backlogged)

        # 深度回落至恢复阈值 4: 解除让路
        depths["observation"] = 4
        backlogged, reason = checker()
        self.assertFalse(backlogged)
        self.assertIsNone(reason)

        # 非监控队列 (如 health) 积压不触发让路
        depths["health"] = 100
        backlogged, reason = checker()
        self.assertFalse(backlogged)

        # 报告队列积压同样触发让路
        depths["report"] = 10
        backlogged, reason = checker()
        self.assertTrue(backlogged)
        self.assertIn("judgment backlog in report", reason or "")

        # 未配置恢复阈值时默认取阈值的一半 (5): 深度 6 保持让路, 5 解除。
        depths.update(observation=0, report=0, health=0)
        default_recovery = JudgmentBacklogChecker(provider, threshold=10)
        depths["observation"] = 10
        self.assertTrue(default_recovery()[0])
        depths["observation"] = 6
        self.assertTrue(default_recovery()[0])
        depths["observation"] = 5
        self.assertFalse(default_recovery()[0])

        # 非法阈值校验
        with self.assertRaises(ValueError):
            JudgmentBacklogChecker(provider, threshold=0)
        with self.assertRaises(ValueError):
            JudgmentBacklogChecker(provider, threshold=10, recovery_threshold=10)
        with self.assertRaises(ValueError):
            JudgmentBacklogChecker(provider, threshold=10, recovery_threshold=15)
        with self.assertRaises(ValueError):
            JudgmentBacklogChecker(provider, threshold=10, recovery_threshold=-1)

    def test_auto_yield_interrupts_encode_and_releases_camera_directory(self) -> None:
        """让路中断在飞编码并释放录像目录占用, 让证据切片不被压缩让路一起拖住。"""
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)
        backlogged = False
        reason = "judgment backlog in observation (depth 12 >= threshold 10)"

        def backlog_signal() -> tuple[bool, str | None]:
            return (backlogged, reason if backlogged else None)

        worker = RecordingCompressionWorker(
            configuration=replace(self.configuration(), readrate=0.5),
            interval=1.0,
            backlog_check=backlog_signal,
        )
        results = []
        thread = Thread(target=lambda: results.append(worker.drain()))
        thread.start()
        try:
            self.wait_state(worker, "running")
            pid = worker.status().pid
            assert pid is not None

            # 真实子进程在飞时触发积压: 本轮中断并收割编码进程, 原件不受影响。
            backlogged = True
            with self.assertLogs("edge_runtime", level="INFO") as captured:
                thread.join(15)
            self.assertTrue(
                any(
                    "edge.recording_compression.yielded" in line
                    and "judgment backlog in observation" in line
                    for line in captured.output
                ),
                captured.output,
            )
            self.assertFalse(thread.is_alive())
            self.assertEqual((results[0].created, results[0].replaced), (0, 0))
            self.assertFalse(Path(f"/proc/{pid}").exists())
            self.assertEqual(digest(source), original)
            self.assertEqual(list(directory.glob(".*.candidate.mp4")), [])
            self.assertIsNone(validated_candidate(source))

            # 让路必须立刻交回录像目录排他占用: 否则阻塞式切片会在整个积压期间等锁。
            with recording_material_access(directory, exclusive=True):
                pass
            with recording_material_access(self.recording, exclusive=True):
                pass

            status = worker.status()
            self.assertEqual(status.state, "paused")
            self.assertEqual(status.requested, "run")
            self.assertIn("auto-yield: judgment backlog in observation", status.detail or "")

            # 让路期间重复 drain 不启动编码, 也不争用资源。
            self.assertEqual(worker.drain().created, 0)

            # 积压解除后按普通一轮重新开始并完成安装。
            backlogged = False
            with self.assertLogs("edge_runtime", level="INFO") as resumed:
                summary = worker.drain()
            self.assertTrue(
                any("edge.recording_compression.resumed" in line for line in resumed.output),
                resumed.output,
            )
            self.assertEqual((summary.created, summary.replaced, summary.failed), (1, 1, 0))
            self.assertEqual(recording_segment_generation(source), "compressed")
            self.assertEqual(worker.status().state, "idle")
            self.assertIsNone(worker.status().detail)
        finally:
            worker.abort()
            thread.join(5)

    def test_operator_pause_is_not_undone_by_backlog_clearing(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        backlogged = True

        def backlog_signal() -> tuple[bool, str | None]:
            return (backlogged, "test backlog" if backlogged else None)

        worker = RecordingCompressionWorker(
            configuration=replace(self.configuration(), readrate=0.5),
            interval=1.0,
            backlog_check=backlog_signal,
        )
        try:
            # 让路期间的显式 pause 固化为操作者意图。
            self.assertEqual(worker.drain().created, 0)
            self.assertEqual(worker.status().state, "paused")
            self.assertEqual(worker.pause().requested, "pause")
            # 让路原因在操作者暂停期间仍可读, 不被 pause 覆盖掉。
            self.assertIn("auto-yield: test backlog", worker.status().detail or "")

            # 积压解除并重新采样到解除事件后仍保持操作者意图, 不自动开始编码。
            backlogged = False
            with self.assertLogs("edge_runtime", level="INFO") as resumed:
                self.assertEqual(worker.drain().created, 0)
            self.assertTrue(
                any("edge.recording_compression.resumed" in line for line in resumed.output),
                resumed.output,
            )
            status = worker.status()
            self.assertEqual(status.state, "paused")
            self.assertEqual(status.requested, "pause")
            self.assertIsNone(status.detail)
            self.assertEqual(recording_segment_generation(source), "original")
        finally:
            worker.abort()

    def test_drain_skips_starting_when_backlogged(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        worker = RecordingCompressionWorker(
            configuration=self.configuration(),
            interval=1.0,
            backlog_check=lambda: (True, "judgment backlog in observation"),
        )
        summary = worker.drain()
        self.assertEqual(summary.created, 0)
        self.assertEqual(summary.replaced, 0)
        status = worker.status()
        self.assertEqual(status.state, "paused")
        self.assertEqual(status.requested, "run")
        self.assertIn("auto-yield: judgment backlog in observation", status.detail or "")

    def test_backlog_signal_without_reason_renders_generic_detail(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        worker = RecordingCompressionWorker(
            configuration=self.configuration(),
            interval=1.0,
            backlog_check=lambda: (True, None),
        )
        self.assertEqual(worker.drain().created, 0)
        status = worker.status()
        self.assertEqual(status.state, "paused")
        self.assertEqual(status.detail, "auto-yield: judgment backlog")

    def test_backlog_signal_failure_keeps_the_previous_yield(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        outcomes: list[tuple[bool, str | None] | Exception] = [
            (True, "judgment backlog in observation"),
            RuntimeError("state unavailable"),
        ]

        def backlog_signal() -> tuple[bool, str | None]:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        worker = RecordingCompressionWorker(
            configuration=self.configuration(),
            interval=1.0,
            backlog_check=backlog_signal,
        )
        with self.assertLogs("edge_runtime", level="WARNING") as captured:
            self.assertEqual(worker.drain().created, 0)
            self.assertEqual(worker.drain().created, 0)
        self.assertTrue(
            any(
                "edge.recording_compression.backlog_check_failed" in line
                for line in captured.output
            ),
            captured.output,
        )
        # 采样失败保持上一轮判定: 仍然让路, 原因不被错误覆盖。
        status = worker.status()
        self.assertEqual(status.state, "paused")
        self.assertIn("auto-yield: judgment backlog in observation", status.detail or "")

    def test_abort_reaps_stopped_process_preserves_source_and_restart_resumes(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)
        worker = RecordingCompressionWorker(
            configuration=replace(self.configuration(), readrate=0.5), interval=1.0
        )
        results = []
        thread = Thread(target=lambda: results.append(worker.drain()))
        thread.start()
        try:
            self.wait_state(worker, "running")
            worker.pause()
            self.wait_state(worker, "paused")
            pid = worker.status().pid
            assert pid is not None
            worker.abort()
            worker.abort()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(Path(f"/proc/{pid}").exists())
            self.assertEqual(worker.status().state, "aborted")
            self.assertEqual((results[0].replaced, results[0].failed), (0, 0))
            self.assertEqual(digest(source), original)
            self.assertEqual(list(directory.glob(".*.candidate.mp4")), [])
            self.assertIsNone(validated_candidate(source))
            restarted = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
            self.assertEqual(restarted.status().state, "aborted")
            self.assertEqual(restarted.drain().created, 0)
            # 崩溃遗留半成品不能成为安全候选; 只在原件已确认后重做。
            partial = directory / f".{source.name}.orphan.candidate.mp4"
            partial.write_bytes(b"incomplete")
            restarted.resume()
            self.assertEqual(restarted.drain().replaced, 1)
            self.assertFalse(partial.exists())
            self.assertEqual(recording_segment_generation(source), "compressed")
        finally:
            worker.abort()
            thread.join(5)

    def test_owner_crash_retains_process_occupancy_until_safe_restart(self) -> None:
        for intent in ("run", "pause", "abort"):
            with self.subTest(intent=intent):
                self.recording = self.root / intent
                self.check_owner_crash(intent)

    def check_owner_crash(self, intent: str) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)
        marker = self.root / f"encoder-pid-{intent}"
        script = """
import sys
from pathlib import Path
from threading import Thread
from time import sleep
from edge_runtime.recording_compression import (
    RecordingCompressionConfiguration, RecordingCompressionWorker,
)
root, marker = map(Path, sys.argv[1:])
configuration = RecordingCompressionConfiguration(
    root, Path('/usr/bin/ffmpeg'), Path('/usr/bin/ffprobe'),
    3600, 2, 30, 60.0, None, readrate=0.05,
)
worker = RecordingCompressionWorker(configuration=configuration, interval=1.0)
thread = Thread(target=worker.drain)
thread.start()
while worker.status().pid is None:
    sleep(0.01)
marker.write_text(str(worker.status().pid))
thread.join()
"""
        owner = subprocess.Popen([sys.executable, "-c", script, str(self.recording), str(marker)])
        encoder: int | None = None
        try:
            deadline = monotonic() + 10
            while not marker.exists() and monotonic() < deadline:
                sleep(0.01)
            self.assertTrue(marker.exists())
            encoder = int(marker.read_text())
            os.killpg(encoder, signal.SIGSTOP)
            # 模拟持久化意图后、内核确认前拥有者崩溃; 新实例不能把意图当作进程事实。
            (self.recording / ".compression-control.json").write_text(
                json.dumps({"version": 1, "requested": intent})
            )
            owner.kill()
            owner.wait(timeout=5)
            restarted = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
            self.assertEqual(restarted.status().state, "busy")
            self.assertEqual(restarted.drain().created, 0)
            self.assertEqual(restarted.status().state, "busy")
            self.assertEqual(digest(source), original)
            # 旧子进程的占用同时保护独立 S047 入口和 S048 替换入口。
            self.assertEqual(compress_aged_segments(self.configuration()).created, 0)
            os.killpg(encoder, signal.SIGKILL)
            deadline = monotonic() + 5
            while restarted.status().state == "busy" and monotonic() < deadline:
                sleep(0.02)
            restarted.resume()
            result = CompressionSummary(0, 0, 0)
            while monotonic() < deadline:
                result = restarted.drain()
                if result.replaced:
                    break
                sleep(0.02)
            self.assertEqual(result.replaced, 1)
            self.assertEqual(list(directory.glob(".*.candidate.mp4")), [])
            self.assertEqual(recording_segment_generation(source), "compressed")
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=5)
            if encoder is not None:
                with suppress(ProcessLookupError):
                    os.killpg(encoder, signal.SIGKILL)

    def test_busy_clears_after_other_owner_releases_an_empty_directory(self) -> None:
        self.recording.mkdir()
        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        with recording_material_access(self.recording, exclusive=True):
            self.assertEqual(worker.drain().created, 0)
            self.assertEqual(worker.status().state, "busy")
        self.assertEqual(worker.drain().created, 0)
        self.assertEqual(worker.status().state, "idle")

    def test_recovery_keeps_only_surviving_material_when_source_is_lost_or_corrupt(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        self.assertEqual(compress_aged_segments(self.configuration()).created, 1)
        candidate = source.with_suffix(".compressed.mp4")
        provenance = source.with_suffix(".compressed.json")
        partial = directory / f".{source.name}.orphan.candidate.mp4"
        partial.write_bytes(candidate.read_bytes())
        expected = {path: path.read_bytes() for path in (candidate, provenance, partial)}
        source.unlink()
        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        self.assertEqual(worker.drain().created, 0)
        source.write_bytes(b"corrupt original")
        self.assertEqual(worker.drain().failed, 1)
        for path, content in expected.items():
            self.assertEqual(path.read_bytes(), content)
        decode(candidate)
        decode(partial)

    def test_corrupt_candidate_with_matching_stats_is_redone_from_the_original(self) -> None:
        """便宜判定认为候选未变、逐字节复核不成立时丢弃候选, 下一轮从原件重做。"""
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)
        self.assertEqual(compress_aged_segments(self.configuration()).created, 1)
        candidate = source.with_suffix(".compressed.mp4")
        damaged = bytearray(candidate.read_bytes())
        damaged[-1] ^= 0xFF
        stat = candidate.stat()
        # 同一尺寸/写入时刻的坏字节: 不逐字节复核就永远不会重做该分段。
        candidate.write_bytes(bytes(damaged))
        os.utime(candidate, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        refused = worker.drain()
        self.assertEqual((refused.replaced, refused.failed), (0, 1))
        self.assertEqual(digest(source), original)
        self.assertFalse(candidate.exists())
        redone = worker.drain()
        self.assertEqual(redone.replaced, 1)
        self.assertEqual(recording_segment_generation(source), "compressed")
        decode(source)

    def test_shutdown_while_paused_reaps_process_and_preserves_pause_on_restart(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)
        worker = RecordingCompressionWorker(
            configuration=replace(self.configuration(), readrate=0.5), interval=0.1
        )
        stop = Event()
        thread = Thread(target=lambda: worker.run_forever(should_stop=stop.is_set))
        thread.start()
        try:
            self.wait_state(worker, "running")
            worker.pause()
            self.wait_state(worker, "paused")
            # 已完成的重复暂停仍显示 paused。
            self.assertEqual(worker.pause().state, "paused")
            stop.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(worker.status().pid)
            self.assertEqual(digest(source), original)
            restarted = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
            self.assertEqual(restarted.status().state, "paused")
            restarted.resume()
            self.assertEqual(restarted.drain().replaced, 1)
        finally:
            stop.set()
            thread.join(5)

    def test_corrupt_control_intent_is_diagnosable_and_only_explicit_resume_repairs_it(
        self,
    ) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)
        (self.recording / ".compression-control.json").write_text("{incomplete")
        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        self.assertEqual(worker.status().state, "unavailable")
        self.assertIn("unreadable", worker.status().detail or "")
        self.assertEqual(worker.drain().created, 0)
        self.assertEqual(digest(source), original)
        worker.resume()
        self.assertEqual(worker.drain().replaced, 1)

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

    def test_worker_atomically_installs_the_verified_candidate_and_reuses_generation(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)
        compress_aged_segments(self.configuration())
        candidate = validated_candidate(source)
        assert candidate is not None
        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)

        summary = worker.drain()

        self.assertEqual(summary.failed, 0)
        self.assertEqual(digest(source), candidate.candidate_sha256)
        self.assertNotEqual(digest(source), original)
        self.assertFalse(source.with_suffix(".compressed.mp4").exists())
        self.assertEqual(recording_segment_generation(source), "compressed")
        decode(source)
        # 模拟安装后、清理前崩溃; 重启仍须回收已能确认安全的旧临时产物。
        partial = directory / f".{source.name}.orphan.candidate.mp4"
        partial.write_bytes(b"unfinished")
        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        second = worker.drain()
        self.assertFalse(partial.exists())
        self.assertEqual((second.created, second.failed), (0, 0))
        self.assertEqual(digest(source), candidate.candidate_sha256)

    def test_installed_material_requires_the_complete_validated_provenance(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        self.assertEqual(worker.drain().replaced, 1)
        provenance = source.with_suffix(".compressed.json")
        document = json.loads(provenance.read_text(encoding="utf-8"))
        for field in ("crf", "candidate_frames", "candidate_mtime_ns"):
            with self.subTest(field=field):
                incomplete = {key: value for key, value in document.items() if key != field}
                provenance.write_text(json.dumps(incomplete), encoding="utf-8")
                with self.assertRaises(EvidenceMediaError) as caught:
                    recording_segment_generation(source)
                self.assertEqual(caught.exception.reason, "material_provenance_invalid")

    def test_replacement_waits_for_reslice_and_preserves_finalized_evidence(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, ANCHOR - 6, 7)
        compress_aged_segments(self.configuration())
        originals = {
            path: digest(path)
            for path in directory.glob("*.mp4")
            if not path.name.endswith(".compressed.mp4")
        }
        state = self._state()
        self.addCleanup(state.close)
        self._enqueue(state)
        (pending,) = state.station(STATION).pending_evidence()
        configuration = EvidenceMediaConfiguration(self.root / "evidence", FFPROBE, 30.0, None)
        reading, release = Event(), Event()
        results: list[str | BaseException] = []
        run = subprocess.run

        def paused_run(*args: object, **kwargs: object) -> object:
            argv = cast(list[str], args[0])
            if "concat" in argv:
                reading.set()
                if not release.wait(10):
                    raise TimeoutError("reader was not released")
            return run(*args, **kwargs)  # type: ignore[call-overload]

        def read() -> None:
            try:
                results.append(
                    slice_evidence(
                        pending,
                        host_id="host-01",
                        recording_directory=self.recording,
                        ffmpeg_binary=FFMPEG,
                        configuration=configuration,
                    ).media_json
                )
            except BaseException as error:
                results.append(error)

        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        with patch.object(subprocess, "run", side_effect=paused_run):
            reader = Thread(target=read)
            reader.start()
            try:
                self.assertTrue(reading.wait(10), results)
                summary = worker.drain()
                self.assertEqual(summary.replaced, 0)
                self.assertEqual({path: digest(path) for path in originals}, originals)
            finally:
                release.set()
                reader.join(15)
        self.assertFalse(reader.is_alive())
        self.assertEqual(len(results), 1)
        if isinstance(results[0], BaseException):
            raise results[0]
        (media,) = json.loads(results[0])
        self.assertEqual(media["material_generation"], "original")
        clip, keyframe = _files(configuration.evidence_directory, media)
        evidence_hashes = (digest(clip), digest(keyframe))

        summary = worker.drain()

        self.assertEqual(summary.replaced, 7)
        self.assertEqual((digest(clip), digest(keyframe)), evidence_hashes)
        decode(clip)
        self.assertEqual({recording_segment_generation(path) for path in originals}, {"compressed"})

    def test_replace_failure_keeps_both_files_and_restart_consumes_the_same_candidate(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        original = digest(source)
        compress_aged_segments(self.configuration())
        candidate = validated_candidate(source)
        assert candidate is not None
        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        with (
            patch.object(os, "replace", side_effect=OSError("replace denied")),
            self.assertLogs("edge_runtime", level="ERROR") as logs,
        ):
            failed = worker.drain()
        self.assertEqual((failed.replaced, failed.failed), (0, 1))
        self.assertTrue(any("replace_failed" in line for line in logs.output))
        self.assertEqual(digest(source), original)
        self.assertEqual(validated_candidate(source), candidate)
        # 旧旁车不能覆盖与当前字节绑定的 S047 原件事实。
        source.with_suffix(".material.json").write_text(
            json.dumps({"generation": "compressed"}), encoding="utf-8"
        )
        self.assertEqual(recording_segment_generation(source), "original")

        restarted = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        self.assertEqual(restarted.drain().replaced, 1)
        self.assertEqual(digest(source), candidate.candidate_sha256)
        self.assertEqual(recording_segment_generation(source), "compressed")

    def test_sync_failure_leaves_a_complete_original_or_committed_candidate(self) -> None:
        sync = os.fsync
        for boundary in ("before", "after"):
            with self.subTest(boundary=boundary):
                directory = self.recording / boundary
                segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
                source = next(directory.glob("*.mp4"))
                compress_aged_segments(self.configuration())
                candidate = validated_candidate(source)
                assert candidate is not None

                def failed_sync(
                    descriptor: int, *, material: Path = source, after: bool = boundary == "after"
                ) -> None:
                    committed = not material.with_suffix(".compressed.mp4").exists()
                    if committed == after:
                        raise OSError("sync failed")
                    sync(descriptor)

                with (
                    patch.object(os, "fsync", side_effect=failed_sync),
                    self.assertRaisesRegex(RecordingCompressionError, "replace_failed"),
                ):
                    replace_validated_candidate(source)
                self.assertEqual(
                    digest(source),
                    candidate.source_sha256 if boundary == "before" else candidate.candidate_sha256,
                )
                self.assertEqual(
                    recording_segment_generation(source),
                    "original" if boundary == "before" else "compressed",
                )
                decode(source)
                self.assertEqual(
                    RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
                    .drain()
                    .failed,
                    0,
                )
                self.assertEqual(digest(source), candidate.candidate_sha256)

    def test_process_exit_on_either_side_of_rename_leaves_complete_identifiable_material(
        self,
    ) -> None:
        for boundary in ("before", "after"):
            with self.subTest(boundary=boundary):
                directory = self.recording / boundary
                segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
                source = next(directory.glob("*.mp4"))
                compress_aged_segments(self.configuration())
                candidate = validated_candidate(source)
                assert candidate is not None
                script = (
                    "import os, sys\n"
                    "from pathlib import Path\n"
                    "from edge_runtime.recording_compression import replace_validated_candidate\n"
                    "rename = os.replace\n"
                    "def interrupted(src, dst):\n"
                    "    if sys.argv[2] == 'after':\n"
                    "        rename(src, dst)\n"
                    "    os._exit(73)\n"
                    "os.replace = interrupted\n"
                    "replace_validated_candidate(Path(sys.argv[1]))\n"
                )
                completed = subprocess.run(
                    [sys.executable, "-c", script, str(source), boundary], check=False
                )
                self.assertEqual(completed.returncode, 73)
                self.assertEqual(
                    digest(source),
                    candidate.source_sha256 if boundary == "before" else candidate.candidate_sha256,
                )
                self.assertEqual(
                    recording_segment_generation(source),
                    "original" if boundary == "before" else "compressed",
                )
                decode(source)
                worker = RecordingCompressionWorker(
                    configuration=self.configuration(), interval=1.0
                )
                self.assertEqual(worker.drain().failed, 0)
                self.assertEqual(digest(source), candidate.candidate_sha256)
                self.assertEqual(recording_segment_generation(source), "compressed")

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

    def test_provenance_failure_after_source_loss_keeps_the_complete_candidate(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))

        def fail_sync(descriptor: int) -> None:
            source.unlink()
            raise OSError("synthetic provenance sync failure")

        with patch("os.fsync", side_effect=fail_sync):
            self.assertEqual(compress_aged_segments(self.configuration()).failed, 1)
        self.assertFalse(source.exists())
        candidate = source.with_suffix(".compressed.mp4")
        self.assertTrue(candidate.exists())
        decode(candidate)

    def test_encoder_failure_after_source_loss_preserves_its_only_output(self) -> None:
        directory = self.recording / CAMERA
        segments(directory, time() - 3 * RAW_WINDOW_SECONDS, 1)
        source = next(directory.glob("*.mp4"))
        encoder = self.root / "source-expiring-encoder"
        encoder.write_text(
            f"#!{sys.executable}\n"
            "import subprocess, sys\n"
            "from pathlib import Path\n"
            "subprocess.run(['/usr/bin/ffmpeg', *sys.argv[1:]], check=True)\n"
            "Path(sys.argv[sys.argv.index('-i') + 1]).unlink()\n"
            "sys.exit(1)\n"
        )
        encoder.chmod(0o700)
        self.assertEqual(compress_aged_segments(self.configuration(ffmpeg=encoder)).failed, 1)
        self.assertFalse(source.exists())
        remaining = list(directory.glob(f".{source.name}.*.candidate.mp4"))
        self.assertEqual(len(remaining), 1)
        decode(remaining[0])
        worker = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        self.assertEqual(worker.drain().created, 0)
        self.assertTrue(remaining[0].exists())

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
        original = digest(source)
        candidate.write_bytes(b"not a video")
        self.assertIsNone(validated_candidate(source))
        with self.assertRaises(RecordingCompressionError):
            replace_validated_candidate(source)
        self.assertEqual(digest(source), original)

        recovered = compress_aged_segments(self.configuration())

        self.assertEqual((recovered.created, recovered.failed), (1, 0))
        self.assertIsNotNone(validated_candidate(source))
        decode(candidate)

        # 同尺寸覆盖: 字节数不变而摘要已变, 未变化判定必须靠留痕记录的写入时刻发现它。
        length = candidate.stat().st_size
        candidate.write_bytes(b"\0" * length)
        self.assertIsNone(validated_candidate(source))
        with self.assertRaises(RecordingCompressionError):
            replace_validated_candidate(source)
        self.assertEqual(digest(source), original)
        overwritten = compress_aged_segments(self.configuration())
        self.assertEqual((overwritten.created, overwritten.failed), (1, 0))
        repaired_candidate = validated_candidate(source)
        assert repaired_candidate is not None
        self.assertEqual(digest(candidate), repaired_candidate.candidate_sha256)
        self.assertNotEqual(candidate.read_bytes(), b"\0" * length)

        # 留痕里的 CRF 越界使代次无法证明; 保留素材并拒绝, 不能删除留痕后当原始素材重做。
        provenance = source.with_suffix(".compressed.json")
        document = json.loads(provenance.read_text(encoding="utf-8"))
        document["crf"] = 99
        provenance.write_text(json.dumps(document), encoding="utf-8")
        self.assertIsNone(validated_candidate(source))
        rejected = compress_aged_segments(self.configuration())
        self.assertEqual((rejected.created, rejected.failed), (0, 1))
        self.assertEqual(digest(source), original)
        self.assertEqual(digest(candidate), repaired_candidate.candidate_sha256)
        self.assertEqual(json.loads(provenance.read_text())["crf"], 99)
        # 恢复权威留痕后仍可复用同一候选。
        document["crf"] = CRF
        provenance.write_text(json.dumps(document), encoding="utf-8")
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
        compression = RecordingCompressionWorker(configuration=self.configuration(), interval=1.0)
        self.assertEqual(compression.drain().replaced, 7)
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
