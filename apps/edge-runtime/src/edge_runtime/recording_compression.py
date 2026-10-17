"""老化录像重编码、真实校验与同目录原子替换 (§5.19)。

候选生成入口保留 S047 契约; 后台 worker 消费同一校验留痕并替换分段。候选和留痕先持久化,
再以一次 rename 提交文件, 代次由留痕与实际字节共同确定。证据媒体本身不重编码。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from math import ceil, isfinite
from pathlib import Path
from threading import Lock, RLock
from time import monotonic, sleep, time
from typing import Literal, cast

from edge_runtime.evidence_media import (
    MATERIAL_GENERATION_COMPRESSED,
    EvidenceMediaError,
    file_sha256,
    recording_material_access,
    recording_segment_generation,
    recording_segment_start,
)
from edge_runtime.local_state import QueueStatus
from edge_runtime.media import DEFAULT_RECORDING_COMPRESSION_BACKLOG_THRESHOLD
from edge_runtime.recording_provenance import (
    RecordingCompressionCandidate,
    read_compression_provenance,
)

_logger = logging.getLogger("edge_runtime")

_CANDIDATE_SUFFIX = ".compressed.mp4"
_PROVENANCE_SUFFIX = ".compressed.json"
_ENCODER = "libx264"
_PRESET = "veryfast"
_FFMPEG_HEAD = ("-hide_banner", "-loglevel", "error")
# 积压信号采样间隔: 秒级即可跟住判定积压, 又不与判定写路径争用 SQLite 锁。
_BACKLOG_POLL_SECONDS = 1.0
_FFPROBE_ARGS = (
    "-v",
    "error",
    "-count_frames",
    "-select_streams",
    "v:0",
    "-show_entries",
    "format=duration:stream=codec_name,nb_read_frames",
    "-of",
    "json",
)
# 重编码不改变时间轴与帧数, 但容器时长取整和首尾帧计数会差一帧左右; 容差按比例并设下限。
# 这不是画质指标 (§5.19: 复核可用性由人判断), 只用来拒绝明显损坏或不完整的产物。
_DURATION_TOLERANCE_RATIO = 0.01
_DURATION_TOLERANCE_MIN_SECONDS = 0.5
_FRAME_TOLERANCE_RATIO = 0.01
_FRAME_TOLERANCE_MIN_FRAMES = 2


class RecordingCompressionError(RuntimeError):
    """一次候选压缩失败的具体原因; 原件保持不动, 候选被移除, 不冒充成功。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class RecordingCompressionConfiguration:
    """本机录像压缩的目录、编码参数、超时与录像时区。

    ``compression_age_seconds`` 是原始素材窗口 (§5.19): 整段录像时间都在它之外的分段才可压缩。
    ``segment_duration_seconds`` 是录像生产者配置的分段时长, 用来在不逐个探测分段的前提下判定
    "整段已老化", 因此判定偏保守。
    """

    recording_directory: Path
    ffmpeg_binary: Path
    ffprobe_binary: Path
    compression_age_seconds: int
    segment_duration_seconds: int
    crf: int
    timeout_seconds: float
    recording_timezone: str | None
    readrate: float | None = None


@dataclass(frozen=True, slots=True)
class CompressionSummary:
    """本轮候选生成与替换计数; 替换后的持久化失败也计入 failed 并告警。"""

    created: int
    skipped: int
    failed: int
    replaced: int = 0


@dataclass(frozen=True, slots=True)
class CompressionStatus:
    """本机控制调用方读取的实际状态; 请求态不等于子进程已停。"""

    state: str
    requested: str
    segment: str | None
    pid: int | None
    detail: str | None = None


class _CompressionInterruptedError(Exception):
    """控制中止本轮, 由拥有者收割进程并清理本次临时文件。"""


class JudgmentBacklogChecker:
    """按 S030 持久队列深度与滞后恢复判定自动让路状态。

    当任一受监控队列 (默认 observation 与 report) 的未确认深度达到 threshold 时触发让路;
    只有当所有受监控队列深度均回落到 recovery_threshold 以下时才解除让路, 避免状态抖动。
    """

    def __init__(
        self,
        provider: Callable[[], tuple[QueueStatus, ...]],
        *,
        threshold: int = DEFAULT_RECORDING_COMPRESSION_BACKLOG_THRESHOLD,
        recovery_threshold: int | None = None,
        monitored_queues: tuple[str, ...] = ("observation", "report"),
    ) -> None:
        if threshold <= 0:
            raise ValueError("backlog threshold must be a positive integer")
        resolved_recovery = (
            recovery_threshold if recovery_threshold is not None else max(0, threshold // 2)
        )
        if resolved_recovery < 0 or resolved_recovery >= threshold:
            raise ValueError(
                "backlog recovery threshold must be non-negative and strictly less than threshold"
            )
        self._provider = provider
        self._threshold = threshold
        self._recovery_threshold = resolved_recovery
        self._monitored_queues = monitored_queues
        self._active = False
        self._active_reason: str | None = None

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def recovery_threshold(self) -> int:
        return self._recovery_threshold

    @property
    def monitored_queues(self) -> tuple[str, ...]:
        return self._monitored_queues

    def __call__(self) -> tuple[bool, str | None]:
        statuses = {status.name: status for status in self._provider()}
        if self._active:
            still_backlogged = any(
                name in statuses and statuses[name].depth > self._recovery_threshold
                for name in self._monitored_queues
            )
            if not still_backlogged:
                self._active = False
                self._active_reason = None
                return False, None
            return True, self._active_reason

        for name in self._monitored_queues:
            status = statuses.get(name)
            if status is not None and status.depth >= self._threshold:
                self._active = True
                self._active_reason = (
                    f"judgment backlog in {name} "
                    f"(depth {status.depth} >= threshold {self._threshold})"
                )
                return True, self._active_reason
        return False, None


@dataclass(frozen=True, slots=True)
class _Probe:
    """ffprobe 一次真实测量: 视频时长、编码格式与完整解码后的帧数。"""

    duration: float
    codec: str
    frames: int


def compress_aged_segments(
    configuration: RecordingCompressionConfiguration,
    *,
    now: float | None = None,
) -> CompressionSummary:
    """S047 候选生成入口; 保留原件, 不安装候选。"""
    return _process_aged_segments(configuration, now=now, install=False)


def _process_aged_segments(
    configuration: RecordingCompressionConfiguration,
    *,
    now: float | None,
    install: bool,
    control: RecordingCompressionWorker | None = None,
) -> CompressionSummary:
    current = time() if now is None else now
    if not isfinite(current):
        raise ValueError("compression clock must be finite")
    cutoff = current - configuration.compression_age_seconds
    created = skipped = failed = replaced = 0
    for segment in _aged_segments(configuration, cutoff=cutoff):
        try:
            with recording_material_access(segment.parent, exclusive=True) as material_fd:
                if control is not None:
                    control._material_fd = material_fd
                    control._segment = segment.name
                    if not control._ready():
                        break
                # 先证明源文件代次, 再判断是否重做候选; 源损坏时保留所有剩余素材。
                if _already_archived(segment):
                    _discard_abandoned_outputs(segment)
                    skipped += 1
                    continue
                current_candidate = _candidate_path(segment).exists() and _candidate_is_current(
                    segment, configuration
                )
                if current_candidate:
                    skipped += 1
                else:
                    _compress_segment(segment, configuration, now=current, control=control)
                    created += 1
                if control is not None and not control._ready():
                    break
                if install:
                    try:
                        replaced += _install_validated_candidate(segment)
                    except RecordingCompressionError as error:
                        if error.reason != "candidate_invalid":
                            raise
                        # 便宜判定认为候选未变而逐字节复核不成立: 丢弃不可验证的候选,
                        # 让下一轮从原件重做, 而不是每个周期重复同一失败。
                        _discard_candidate(segment)
                        raise
                    # 只有完整素材已验证并安装后才回收崩溃拥有者留下的临时产物。
                    _discard_abandoned_outputs(segment)
        except _CompressionInterruptedError:
            break
        except BlockingIOError:
            skipped += 1
            continue
        except OSError as error:
            failed += 1
            _log_failure(segment.name, "io_failed", str(error))
            continue
        except (EvidenceMediaError, RecordingCompressionError) as error:
            failed += 1
            _log_failure(segment.name, error.reason, error.detail)
            continue
    return CompressionSummary(created=created, skipped=skipped, failed=failed, replaced=replaced)


def validated_candidate(segment: Path) -> RecordingCompressionCandidate | None:
    """消费边界: 留痕与当前字节都匹配时才返回候选, 否则返回 None, 调用方不得替换。"""
    candidate = _candidate_path(segment)
    provenance = _provenance_path(segment)
    if not candidate.is_file() or not provenance.is_file():
        return None
    try:
        document = read_compression_provenance(provenance)
    except (OSError, ValueError):
        return None
    if document.source != segment.name or document.candidate != candidate.name:
        return None
    try:
        matches = (
            file_sha256(segment) == document.source_sha256
            and file_sha256(candidate) == document.candidate_sha256
        )
    except OSError:
        return None
    return document if matches else None


def replace_validated_candidate(segment: Path) -> bool:
    """仅安装与 S047 校验留痕一致的候选; 原子 rename 前不删除原件。

    文件与同目录留痕先 fsync, 再提交唯一一次替换。重启后无论 rename 是否落盘, 留痕都能
    解释实际分段的代次; 不另建提交账本。返回 False 表示没有候选。
    """
    try:
        with recording_material_access(segment.parent, exclusive=True):
            if _already_archived(segment):
                return False
            return _install_validated_candidate(segment)
    except BlockingIOError:
        return False
    except (OSError, EvidenceMediaError) as error:
        raise RecordingCompressionError("replace_failed", str(error)) from error


def _install_validated_candidate(segment: Path) -> bool:
    """调用方持排他占用且已确认原始代次; 校验一次后完成持久化与替换。"""
    candidate = _candidate_path(segment)
    if not candidate.exists():
        return False
    if segment.is_symlink() or candidate.is_symlink():
        raise RecordingCompressionError("candidate_invalid", "symbolic recording material")
    document = validated_candidate(segment)
    if document is None:
        raise RecordingCompressionError(
            "candidate_invalid", "candidate no longer matches validation"
        )
    try:
        for path in (candidate, _provenance_path(segment)):
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        descriptor = os.open(segment.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
            os.replace(candidate, segment)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise RecordingCompressionError("replace_failed", str(error)) from error
    return True


class RecordingCompressionWorker:
    """后台生成并原子安装已验证候选, 不依赖中心或阻塞判定。"""

    def __init__(
        self,
        *,
        configuration: RecordingCompressionConfiguration,
        interval: float,
        clock: Callable[[], float] = time,
        backlog_check: Callable[[], tuple[bool, str | None]] | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("compression interval must be positive")
        self._configuration = configuration
        self._interval = interval
        self._clock = clock
        self._backlog_check = backlog_check
        self._yielded = False
        self._yield_reason: str | None = None
        self._next_backlog_poll = 0.0
        self._control_lock = RLock()
        self._draining = Lock()
        self._intent_path = configuration.recording_directory / ".compression-control.json"
        self._control_error: str | None = None
        try:
            self._requested = self._read_request()
        except (OSError, ValueError) as error:
            # 不能证明控制意图时只停归档, 不让坏文件终止自治判定。
            self._requested = "abort"
            self._control_error = f"compression control state unreadable: {type(error).__name__}"
        self._state = (
            "unavailable"
            if self._control_error
            else {"run": "idle", "pause": "paused", "abort": "aborted"}[self._requested]
        )
        self._segment: str | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._ownership_fd = self._material_fd = -1
        self._should_stop: Callable[[], bool] = lambda: False
        rate = configuration.readrate
        if rate is not None and (isinstance(rate, bool) or not isfinite(rate) or rate <= 0):
            raise ValueError("compression readrate must be positive and finite")

    def _read_request(self) -> Literal["run", "pause", "abort"]:
        try:
            document = json.loads(self._intent_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return "run"
        if not isinstance(document, dict) or set(document) != {"version", "requested"}:
            raise ValueError("invalid compression control state")
        if document["version"] != 1 or document["requested"] not in ("run", "pause", "abort"):
            raise ValueError("invalid compression control state")
        return cast(Literal["run", "pause", "abort"], document["requested"])

    def _poll_backlog(self, *, force: bool = False) -> None:
        if self._backlog_check is None:
            return
        # 积压信号读的是判定路径共用的 SQLite 连接: 按秒级节流, 不在编码轮询里密集抢占判定写锁。
        # 一轮压缩开始的 drain 不受节流影响: 它本来就按压缩间隔触发。
        current = monotonic()
        if not force and current < self._next_backlog_poll:
            return
        self._next_backlog_poll = current + _BACKLOG_POLL_SECONDS
        try:
            backlogged, reason = self._backlog_check()
        except Exception as error:
            # 压缩侧的积压采样失败不得变成判定侧故障: 保持上一轮判定并告警。
            _logger.warning("edge.recording_compression.backlog_check_failed error=%s", error)
            return
        with self._control_lock:
            if backlogged == self._yielded:
                return
            self._yielded = backlogged
            self._yield_reason = reason if backlogged else None
            _logger.info(
                "edge.recording_compression.%s reason=%s",
                "yielded" if backlogged else "resumed",
                reason if backlogged else "backlog_cleared",
            )

    @property
    def readrate(self) -> float | None:
        return self._configuration.readrate

    def status(self) -> CompressionStatus:
        """供本机管理调用方查询; pausing/resuming/aborting 仅表示意图已接受。"""
        with self._control_lock:
            state, detail = self._state, self._control_error
            directory = self._configuration.recording_directory
            if self._ownership_fd < 0 and self._draining.locked():
                state, detail = "busy", "compression ownership is being checked"
            elif self._ownership_fd < 0 and directory.is_dir():
                try:
                    with recording_material_access(directory, exclusive=True):
                        if state == "busy":
                            state = {"run": "idle", "pause": "paused", "abort": "aborted"}[
                                self._requested
                            ]
                except BlockingIOError:
                    # 存活的旧子进程仍持占用时, 持久化意图不能证明实际进程已停止。
                    state, detail = "busy", "another compression owner still holds the directory"
                except OSError as error:
                    state, detail = "unavailable", f"compression ownership unreadable: {error}"
            if detail is None and self._yielded and self._requested == "run":
                detail = f"auto-yield: {self._yield_reason}"
                if state in ("idle", "running"):
                    state = "paused"
            return CompressionStatus(
                state,
                self._requested,
                self._segment,
                self._process.pid if self._process is not None else None,
                detail,
            )

    def pause(self) -> CompressionStatus:
        return self._request("pause")

    def resume(self) -> CompressionStatus:
        return self._request("run")

    def abort(self) -> CompressionStatus:
        return self._request("abort")

    def _request(self, requested: Literal["run", "pause", "abort"]) -> CompressionStatus:
        with self._control_lock:
            if requested == self._requested and self._control_error is None:
                return self.status()
            if self._state == "aborting":
                raise RecordingCompressionError("control_pending", "wait for abort confirmation")
            directory = self._configuration.recording_directory
            directory.mkdir(parents=True, exist_ok=True)
            document: dict[str, object] = {"version": 1, "requested": requested}
            if self._ownership_fd >= 0:
                _write_document(self._intent_path, document)
            else:
                with recording_material_access(directory, exclusive=True):
                    _write_document(self._intent_path, document)
            self._requested = requested
            self._control_error = None
            if self._draining.locked():
                self._state = {"pause": "pausing", "run": "resuming", "abort": "aborting"}[
                    requested
                ]
            else:
                self._state = {"pause": "paused", "run": "idle", "abort": "aborted"}[requested]
            return self.status()

    def drain(self) -> CompressionSummary:
        """同一 worker 与录像目录各只有一个执行者; 不等待其他压缩任务。"""
        empty = CompressionSummary(0, 0, 0)
        # 与 status 的空闲占用探测互斥, 查询不能抢走本对象即将开始的执行占用。
        with self._control_lock:
            if not self._draining.acquire(blocking=False):
                return empty
        try:
            self._poll_backlog(force=True)
            if self._control_error is not None or self._should_stop() or self._yielded:
                return empty
            directory = self._configuration.recording_directory
            if not directory.is_dir():
                return empty
            try:
                with recording_material_access(directory, exclusive=True) as descriptor:
                    self._ownership_fd = descriptor
                    # 其他已完成控制的实例可能比本对象更新; 获得执行占用后重新读取意图。
                    try:
                        self._requested = self._read_request()
                    except (OSError, ValueError) as error:
                        self._control_error = (
                            f"compression control state unreadable: {type(error).__name__}"
                        )
                        self._state = "unavailable"
                        _log_failure(directory.name, "control_state_invalid", self._control_error)
                        return empty
                    if self._requested != "run":
                        return empty
                    result = _process_aged_segments(
                        self._configuration, now=self._clock(), install=True, control=self
                    )
            except BlockingIOError:
                with self._control_lock:
                    self._state = "busy"
                return empty
            except OSError as error:
                _log_failure(directory.name, "io_failed", str(error))
                return CompressionSummary(0, 0, 1)
            if result.created or result.replaced or result.failed:
                _logger.info(
                    "edge.recording_compression.summary created=%s replaced=%s "
                    "skipped=%s failed=%s",
                    result.created,
                    result.replaced,
                    result.skipped,
                    result.failed,
                )
            return result
        finally:
            with self._control_lock:
                self._segment = None
                self._ownership_fd = self._material_fd = -1
                if self._control_error is not None:
                    self._state = "unavailable"
                elif self._yielded and self._requested == "run":
                    self._state = "paused"
                else:
                    self._state = {"run": "idle", "pause": "paused", "abort": "aborted"}[
                        self._requested
                    ]
                self._draining.release()

    def _ready(self) -> bool:
        while True:
            self._poll_backlog()
            with self._control_lock:
                if self._requested == "abort" or self._should_stop():
                    return False
                if self._yielded:
                    # 让路时长由判定积压决定, 不能继续持有录像目录排他占用把切片一起拖住:
                    # 中断本轮让 drain 释放占用, 积压解除后按普通一轮重新开始。
                    return False
                if self._requested == "run":
                    return True
                self._state = "paused"
            sleep(0.05)

    def _execute(
        self, argv: list[str], timeout: float, reason: str
    ) -> subprocess.CompletedProcess[bytes]:
        if not self._ready():
            raise _CompressionInterruptedError
        # 子进程继承目录占用: 拥有者崩溃后仍活着的 FFmpeg 不会与恢复任务重复编码。
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=(self._ownership_fd, self._material_fd),
        )
        with self._control_lock:
            self._process = process
            self._state = {"run": "running", "pause": "pausing", "abort": "aborting"}[
                self._requested
            ]
        active = 0.0
        previous = monotonic()
        stopped = False
        try:
            while True:
                self._poll_backlog()
                with self._control_lock:
                    requested = self._requested
                    yielded = self._yielded
                    interrupt = requested == "abort" or self._should_stop() or yielded
                    current = monotonic()
                    if not stopped:
                        active += current - previous
                    previous = current
                    if interrupt:
                        self._signal(process, signal.SIGKILL)
                    elif requested == "pause" and not stopped:
                        self._signal(process, signal.SIGSTOP)
                    elif requested == "run" and stopped:
                        self._signal(process, signal.SIGCONT)
                    # 只有内核确认停止/继续事件后才发布相应实际状态。
                    pid, status = os.waitpid(process.pid, os.WNOHANG | os.WUNTRACED | os.WCONTINUED)
                    if pid:
                        if os.WIFSTOPPED(status):
                            stopped = True
                            self._state = "paused"
                        elif os.WIFCONTINUED(status):
                            stopped = False
                            self._state = "running"
                        else:
                            process.returncode = os.waitstatus_to_exitcode(status)
                    if not interrupt and active >= timeout:
                        raise RecordingCompressionError(
                            reason, f"command timed out after {timeout:g}s"
                        )
                try:
                    stdout, stderr = process.communicate(timeout=0.05)
                    if interrupt:
                        raise _CompressionInterruptedError
                    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                self._signal(process, signal.SIGKILL)
            process.communicate()
            with self._control_lock:
                self._process = None

    @staticmethod
    def _signal(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        # 已收割的 pid 可能已被复用: 只在子进程仍存活时按进程组发信号。
        if process.poll() is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, sig)

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        self._should_stop = should_stop
        try:
            while not should_stop():
                self.drain()
                deadline = monotonic() + self._interval
                while not should_stop() and monotonic() < deadline:
                    sleep(0.05)
        finally:
            self._should_stop = lambda: False


def _aged_segments(
    configuration: RecordingCompressionConfiguration, *, cutoff: float
) -> list[Path]:
    """按录像墙钟列出整段已出原始素材窗口的分段, 不探测任何分段。

    录像目录不可读只让这一个后台任务本轮失败: 记录告警并返回已列出的分段, 不让媒体目录
    问题结束判定运行时 (edge-autonomy.md §5.7)。
    """
    directory = configuration.recording_directory
    if not directory.is_dir():
        return []
    # 以配置的分段时长作为录像生产者产出的上界, 要求整段都落在窗口之外 (保守判定)。
    latest_start = cutoff - configuration.segment_duration_seconds
    aged: list[Path] = []
    try:
        cameras = sorted(directory.iterdir())
    except OSError as error:
        _log_failure(directory.name, "io_failed", str(error))
        return []
    for camera in cameras:
        if not camera.is_dir():
            continue
        try:
            paths = sorted(camera.glob("*.mp4"))
        except OSError as error:
            _log_failure(camera.name, "io_failed", str(error))
            continue
        for path in paths:
            if path.name.endswith(_CANDIDATE_SUFFIX):
                continue
            start = recording_segment_start(path.name, configuration.recording_timezone)
            if start is not None and start <= latest_start:
                aged.append(path)
    return aged


def _discard_abandoned_outputs(segment: Path) -> None:
    """仅在调用方已确认完整归档素材后回收旧拥有者的临时输出。"""
    for partial in segment.parent.glob(f".{segment.name}.*.candidate.mp4"):
        _discard(partial)


def _discard_candidate(segment: Path) -> None:
    """候选无法通过消费边界复核时丢弃候选与留痕, 原件保持不动。"""
    _discard(_candidate_path(segment))
    _discard(_provenance_path(segment))


def _already_archived(segment: Path) -> bool:
    """已归档为 compressed 的分段不再二次重编码; 代次声明非法时拒绝处理。"""
    return recording_segment_generation(segment) == MATERIAL_GENERATION_COMPRESSED


def _candidate_is_current(segment: Path, configuration: RecordingCompressionConfiguration) -> bool:
    """留痕与当前原件/候选的字节数和写入时刻都一致时跳过重编码。

    这是便宜的"没有变化"判定, 只用来避免重复编码; 候选能否被消费由 `validated_candidate`
    的逐字节复核决定, 两者不是同一条规则。判定不成立也保留候选直到重做成功。它不重算摘要,
    因此只改写留痕里的摘要字段不会触发重编码; 该候选仍无法通过消费边界复核, 不会被替换。
    """
    candidate = _candidate_path(segment)
    provenance = _provenance_path(segment)
    if not candidate.is_file() and not provenance.is_file():
        return False
    try:
        document = read_compression_provenance(provenance)
        candidate_stat = candidate.stat()
        matches = (
            document.source == segment.name
            and document.candidate == candidate.name
            and document.source_size_bytes == segment.stat().st_size
            and document.candidate_size_bytes == candidate_stat.st_size
            and document.candidate_mtime_ns == candidate_stat.st_mtime_ns
            and document.crf == configuration.crf
            and document.encoder == _ENCODER
            and document.preset == _PRESET
        )
    except (OSError, ValueError):
        matches = False
    return matches


def _compress_segment(
    segment: Path,
    configuration: RecordingCompressionConfiguration,
    *,
    now: float,
    control: RecordingCompressionWorker | None = None,
) -> None:
    """编码到临时文件并校验, 通过后先落候选再落留痕; 任一步失败都不留下候选。

    旧候选直到新产物完整验证后才替换; 失败路径只清理本次自己的临时文件。
    """
    candidate = _candidate_path(segment)
    provenance = _provenance_path(segment)
    work: Path | None = None
    # 源文件代次先取证: 原件消失或变化时 finally 必须能判断产物能否安全删除。
    source_stat = segment.stat()
    try:
        work = _temporary_candidate(segment)
        _encode(segment, work, configuration, control=control)
        source_probe = _probe(segment, configuration, control=control)
        candidate_probe = _probe(work, configuration, control=control)
        _require_decodable(work, configuration, control=control)
        _verify(source_probe, candidate_probe)
        document = RecordingCompressionCandidate(
            source=segment.name,
            candidate=candidate.name,
            source_sha256=file_sha256(segment),
            candidate_sha256=file_sha256(work),
            source_size_bytes=segment.stat().st_size,
            candidate_size_bytes=work.stat().st_size,
            # os.replace 保留 mtime, 所以在替换前取临时文件的实际写入时刻即为候选的写入时刻。
            candidate_mtime_ns=work.stat().st_mtime_ns,
            source_duration_seconds=source_probe.duration,
            candidate_duration_seconds=candidate_probe.duration,
            source_frames=source_probe.frames,
            candidate_frames=candidate_probe.frames,
            crf=configuration.crf,
            encoder=_ENCODER,
            preset=_PRESET,
            generation=MATERIAL_GENERATION_COMPRESSED,
            encoded_at=_utc(now),
        )
        os.replace(work, candidate)
        try:
            _write_provenance(provenance, document)
        except OSError:
            # 留痕失败的候选不可安装; 原件被外部回收时仍须保留仅存素材。
            _discard_output_if_source_unchanged(candidate, segment, source_stat)
            raise
    except OSError as error:
        raise RecordingCompressionError("io_failed", str(error)) from error
    finally:
        if work is not None and work.exists():
            _discard_output_if_source_unchanged(work, segment, source_stat)


def _discard_output_if_source_unchanged(
    output: Path, segment: Path, before: os.stat_result
) -> None:
    """外部回收不参与锁; 原件消失或变化时保留可能仅存的产物供诊断。"""
    try:
        current = segment.stat()
        unchanged = all(
            getattr(current, field) == getattr(before, field)
            for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        )
    except OSError:
        unchanged = False
    if unchanged:
        output.unlink(missing_ok=True)
    else:
        _log_failure(segment.name, "source_changed", "uncommitted output retained")


def _encode(
    source: Path,
    destination: Path,
    configuration: RecordingCompressionConfiguration,
    *,
    control: RecordingCompressionWorker | None = None,
) -> None:
    """真实重编码为浏览器可直接回看的 H.264 (默认提高 CRF, 不换 H.265); 音频不重编码。"""
    argv = [
        str(configuration.ffmpeg_binary),
        *_FFMPEG_HEAD,
        "-y",
        *(["-readrate", str(configuration.readrate)] if configuration.readrate is not None else []),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c:v",
        _ENCODER,
        "-crf",
        str(configuration.crf),
        "-preset",
        _PRESET,
        "-pix_fmt",
        "yuv420p",
        "-map",
        "0:a?",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        "-f",
        "mp4",
        str(destination),
    ]
    _spawn(argv, configuration.timeout_seconds, "encode_failed", control=control)


def _probe(
    path: Path,
    configuration: RecordingCompressionConfiguration,
    *,
    control: RecordingCompressionWorker | None = None,
) -> _Probe:
    """测量视频时长/编码/完整解码帧数; 结果不完整即失败, 不猜测量值。"""
    argv = [str(configuration.ffprobe_binary), *_FFPROBE_ARGS, str(path)]
    completed = _spawn(argv, configuration.timeout_seconds, "probe_failed", control=control)
    try:
        payload = cast(dict[str, object], json.loads(completed.stdout))
    except json.JSONDecodeError as error:
        raise RecordingCompressionError(
            "probe_failed", f"unreadable ffprobe output for {path.name}"
        ) from error
    streams = payload.get("streams")
    fmt = payload.get("format")
    if not isinstance(streams, list) or not streams or not isinstance(fmt, dict):
        raise RecordingCompressionError("probe_failed", f"{path.name} has no video stream")
    stream = cast(dict[str, object], streams[0])
    duration = _probe_number(cast(dict[str, object], fmt).get("duration"), path, "duration")
    codec = _probe_text(stream.get("codec_name"), path, "codec_name")
    frames = _probe_frames(stream.get("nb_read_frames"), path)
    if duration <= 0 or frames <= 0:
        raise RecordingCompressionError(
            "probe_failed", f"{path.name} measured zero duration or frames"
        )
    return _Probe(duration=duration, codec=codec, frames=frames)


def _require_decodable(
    path: Path,
    configuration: RecordingCompressionConfiguration,
    *,
    control: RecordingCompressionWorker | None = None,
) -> None:
    """完整解码候选; -xerror 让坏帧以非零退出而不是被容错吞掉。"""
    argv = [
        str(configuration.ffmpeg_binary),
        *_FFMPEG_HEAD,
        "-xerror",
        "-i",
        str(path),
        "-f",
        "null",
        "-",
    ]
    _spawn(argv, configuration.timeout_seconds, "verification_failed", control=control)


def _verify(source: _Probe, candidate: _Probe) -> None:
    """重编码产物必须时长一致、编码为 H.264 且帧数在容差内。"""
    if candidate.codec != "h264":
        raise RecordingCompressionError(
            "verification_failed",
            f"candidate codec {candidate.codec} is not browser-playable H.264",
        )
    duration_tolerance = max(
        _DURATION_TOLERANCE_MIN_SECONDS, source.duration * _DURATION_TOLERANCE_RATIO
    )
    if abs(candidate.duration - source.duration) > duration_tolerance:
        raise RecordingCompressionError(
            "verification_failed",
            f"duration {candidate.duration:.3f}s differs from {source.duration:.3f}s",
        )
    frame_tolerance = max(_FRAME_TOLERANCE_MIN_FRAMES, ceil(source.frames * _FRAME_TOLERANCE_RATIO))
    if abs(candidate.frames - source.frames) > frame_tolerance:
        raise RecordingCompressionError(
            "verification_failed",
            f"frame count {candidate.frames} differs from {source.frames}",
        )


def _spawn(
    argv: list[str],
    timeout: float,
    reason: str,
    *,
    control: RecordingCompressionWorker | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """运行外部编解码命令; 超时、无法启动与非零退出都翻译成该步骤的具体原因。"""
    try:
        completed = (
            subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
            if control is None
            else control._execute(argv, timeout, reason)
        )
    except subprocess.TimeoutExpired as error:
        raise RecordingCompressionError(reason, f"command timed out after {timeout:g}s") from error
    except OSError as error:
        raise RecordingCompressionError(reason, str(error)) from error
    if completed.returncode != 0:
        lines = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        detail = lines[-1] if lines else "no detail"
        raise RecordingCompressionError(reason, f"exit {completed.returncode}: {detail}")
    return completed


def _probe_number(value: object, path: Path, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise RecordingCompressionError("probe_failed", f"{path.name} has no {name}")
    try:
        result = float(value)
    except ValueError as error:
        raise RecordingCompressionError("probe_failed", f"{path.name} has no {name}") from error
    if not isfinite(result):
        raise RecordingCompressionError("probe_failed", f"{path.name} has a non-finite {name}")
    return result


def _probe_text(value: object, path: Path, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecordingCompressionError("probe_failed", f"{path.name} has no {name}")
    return value


def _probe_frames(value: object, path: Path) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise RecordingCompressionError("probe_failed", f"{path.name} has no frame count")
    return int(value)


def _write_provenance(path: Path, document: RecordingCompressionCandidate) -> None:
    """原子写入参数/代次留痕; 同目录 rename 后才可见, 不暴露半份文档。"""
    _write_document(path, document.to_wire())


def _write_document(path: Path, document: dict[str, object]) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _candidate_path(segment: Path) -> Path:
    return segment.with_suffix(_CANDIDATE_SUFFIX)


def _provenance_path(segment: Path) -> Path:
    return segment.with_suffix(_PROVENANCE_SUFFIX)


def _temporary_candidate(segment: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{segment.name}.", suffix=".candidate.mp4", dir=segment.parent
    )
    os.close(descriptor)
    return Path(name)


def _discard(path: Path) -> None:
    """移除候选或留痕; 删除失败是真实故障, 不静默吞掉。"""
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        raise RecordingCompressionError(
            "io_failed", f"cannot remove {path.name}: {error}"
        ) from error


def _log_failure(source: str, reason: str, detail: str) -> None:
    _logger.error(
        "edge.recording_compression.failed source=%s reason=%s detail=%s",
        source,
        reason,
        detail,
    )


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "CompressionStatus",
    "CompressionSummary",
    "JudgmentBacklogChecker",
    "RecordingCompressionCandidate",
    "RecordingCompressionConfiguration",
    "RecordingCompressionError",
    "RecordingCompressionWorker",
    "compress_aged_segments",
    "replace_validated_candidate",
    "validated_candidate",
]
