"""老化录像重编码、真实校验与同目录原子替换 (§5.19)。

候选生成入口保留 S047 契约; 后台 worker 消费同一校验留痕并替换分段。候选和留痕先持久化,
再以一次 rename 提交文件, 代次由留痕与实际字节共同确定。证据媒体本身不重编码。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import ceil, isfinite
from pathlib import Path
from time import monotonic, sleep, time
from typing import cast

from edge_runtime.configuration_values import (
    _crf_value,
    _non_empty_string,
    _non_negative_integer,
    _object,
    _positive_integer,
    _positive_number,
    _require_keys,
)
from edge_runtime.evidence_media import (
    MATERIAL_GENERATION_COMPRESSED,
    EvidenceMediaError,
    file_sha256,
    recording_material_access,
    recording_segment_generation,
    recording_segment_start,
)

_logger = logging.getLogger("edge_runtime")

_CANDIDATE_SUFFIX = ".compressed.mp4"
_PROVENANCE_SUFFIX = ".compressed.json"
_ENCODER = "libx264"
_PRESET = "veryfast"
_FFMPEG_HEAD = ("-hide_banner", "-loglevel", "error")
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


@dataclass(frozen=True, slots=True)
class RecordingCompressionCandidate:
    """已通过真实校验的候选及其编码参数/代次留痕; S048 消费的同一产物。"""

    source: str
    candidate: str
    source_sha256: str
    candidate_sha256: str
    source_size_bytes: int
    candidate_size_bytes: int
    source_duration_seconds: float
    candidate_duration_seconds: float
    source_frames: int
    candidate_frames: int
    candidate_mtime_ns: int
    crf: int
    encoder: str
    preset: str
    generation: str
    encoded_at: str

    def to_wire(self) -> dict[str, object]:
        """转换为本机留痕文档; 文档字段名就是这里的字段名, 只在此处定义一次。"""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CompressionSummary:
    """本轮候选生成与替换计数; 替换后的持久化失败也计入 failed 并告警。"""

    created: int
    skipped: int
    failed: int
    replaced: int = 0


@dataclass(frozen=True, slots=True)
class _Probe:
    """ffprobe 一次真实测量: 视频时长、编码格式与完整解码后的帧数。"""

    duration: float
    codec: str
    frames: int


_PROVENANCE_KEYS = frozenset(RecordingCompressionCandidate.__dataclass_fields__)


def compress_aged_segments(
    configuration: RecordingCompressionConfiguration,
    *,
    now: float | None = None,
) -> CompressionSummary:
    """S047 候选生成入口; 保留原件, 不安装候选。"""
    return _process_aged_segments(configuration, now=now, install=False)


def archive_aged_segments(
    configuration: RecordingCompressionConfiguration, *, now: float | None = None
) -> CompressionSummary:
    """后台归档入口: 生成候选并在同一素材占用内验证、原子安装。"""
    return _process_aged_segments(configuration, now=now, install=True)


def _process_aged_segments(
    configuration: RecordingCompressionConfiguration, *, now: float | None, install: bool
) -> CompressionSummary:
    current = time() if now is None else now
    if not isfinite(current):
        raise ValueError("compression clock must be finite")
    cutoff = current - configuration.compression_age_seconds
    created = skipped = failed = replaced = 0
    for segment in _aged_segments(configuration, cutoff=cutoff):
        try:
            with recording_material_access(segment.parent, exclusive=True):
                if _already_archived(segment):
                    skipped += 1
                    continue
                if _candidate_is_current(segment, configuration):
                    skipped += 1
                else:
                    _compress_segment(segment, configuration, now=current)
                    created += 1
                if install:
                    replaced += _install_validated_candidate(segment)
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
        document = _read_provenance(provenance)
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
    ) -> None:
        if interval <= 0:
            raise ValueError("compression interval must be positive")
        self._configuration = configuration
        self._interval = interval
        self._clock = clock

    def drain(self) -> CompressionSummary:
        """处理一轮; 逐分段失败已在本轮内转为告警, 不中断后续分段。"""
        result = archive_aged_segments(self._configuration, now=self._clock())
        if result.created or result.replaced or result.failed:
            _logger.info(
                "edge.recording_compression.summary created=%s replaced=%s skipped=%s failed=%s",
                result.created,
                result.replaced,
                result.skipped,
                result.failed,
            )
        return result

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            self.drain()
            deadline = monotonic() + self._interval
            while not should_stop() and monotonic() < deadline:
                sleep(0.05)


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


def _already_archived(segment: Path) -> bool:
    """已归档为 compressed 的分段不再二次重编码; 代次声明非法时拒绝处理。"""
    return recording_segment_generation(segment) == MATERIAL_GENERATION_COMPRESSED


def _candidate_is_current(segment: Path, configuration: RecordingCompressionConfiguration) -> bool:
    """留痕与当前原件/候选的字节数和写入时刻都一致时跳过重编码。

    这是便宜的"没有变化"判定, 只用来避免重复编码; 候选能否被消费由 `validated_candidate`
    的逐字节复核决定, 两者不是同一条规则。判定不成立即移除无法证明的候选。它不重算摘要,
    因此只改写留痕里的摘要字段不会触发重编码; 该候选仍无法通过消费边界复核, 不会被替换。
    """
    candidate = _candidate_path(segment)
    provenance = _provenance_path(segment)
    if not candidate.is_file() and not provenance.is_file():
        return False
    try:
        document = _read_provenance(provenance)
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
    if not matches:
        _discard(candidate)
        _discard(provenance)
    return matches


def _compress_segment(
    segment: Path,
    configuration: RecordingCompressionConfiguration,
    *,
    now: float,
) -> None:
    """编码到临时文件并校验, 通过后先落候选再落留痕; 任一步失败都不留下候选。

    到达这里的候选已由 `_candidate_is_current` 判定为不存在或不可证明并移除, 因此本函数不再
    重复清理; 失败路径只清理本次自己的临时文件。
    """
    candidate = _candidate_path(segment)
    provenance = _provenance_path(segment)
    work: Path | None = None
    try:
        work = _temporary_candidate(segment)
        _encode(segment, work, configuration)
        source_probe = _probe(segment, configuration)
        candidate_probe = _probe(work, configuration)
        _require_decodable(work, configuration)
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
            # 留痕失败即候选不可证明: 移除候选, 保持"没有候选"这一真实状态。
            _discard(candidate)
            raise
    except OSError as error:
        raise RecordingCompressionError("io_failed", str(error)) from error
    finally:
        if work is not None:
            work.unlink(missing_ok=True)


def _encode(
    source: Path, destination: Path, configuration: RecordingCompressionConfiguration
) -> None:
    """真实重编码为浏览器可直接回看的 H.264 (默认提高 CRF, 不换 H.265); 音频不重编码。"""
    argv = [
        str(configuration.ffmpeg_binary),
        *_FFMPEG_HEAD,
        "-y",
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
    _spawn(argv, configuration.timeout_seconds, "encode_failed")


def _probe(path: Path, configuration: RecordingCompressionConfiguration) -> _Probe:
    """测量视频时长/编码/完整解码帧数; 结果不完整即失败, 不猜测量值。"""
    argv = [str(configuration.ffprobe_binary), *_FFPROBE_ARGS, str(path)]
    completed = _spawn(argv, configuration.timeout_seconds, "probe_failed")
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


def _require_decodable(path: Path, configuration: RecordingCompressionConfiguration) -> None:
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
    _spawn(argv, configuration.timeout_seconds, "verification_failed")


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


def _spawn(argv: list[str], timeout: float, reason: str) -> subprocess.CompletedProcess[bytes]:
    """运行外部编解码命令; 超时、无法启动与非零退出都翻译成该步骤的具体原因。"""
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
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
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document.to_wire(), stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _read_provenance(path: Path) -> RecordingCompressionCandidate:
    """严格读取留痕; 字段集合或类型不符即拒绝, 不把无法证明的候选当已验证。"""
    document = _object(json.loads(path.read_text(encoding="utf-8")), "compression provenance")
    _require_keys(document, required=_PROVENANCE_KEYS)
    generation = _non_empty_string(document["generation"], "provenance generation")
    if generation != MATERIAL_GENERATION_COMPRESSED:
        raise ValueError("compression provenance generation must be compressed")
    return RecordingCompressionCandidate(
        source=_non_empty_string(document["source"], "provenance source"),
        candidate=_non_empty_string(document["candidate"], "provenance candidate"),
        source_sha256=_non_empty_string(document["source_sha256"], "provenance source_sha256"),
        candidate_sha256=_non_empty_string(
            document["candidate_sha256"], "provenance candidate_sha256"
        ),
        source_size_bytes=_positive_integer(
            document["source_size_bytes"], "provenance source_size_bytes"
        ),
        candidate_size_bytes=_positive_integer(
            document["candidate_size_bytes"], "provenance candidate_size_bytes"
        ),
        candidate_mtime_ns=_non_negative_integer(
            document["candidate_mtime_ns"], "provenance candidate_mtime_ns"
        ),
        source_duration_seconds=_positive_number(
            document["source_duration_seconds"], "provenance source_duration_seconds"
        ),
        candidate_duration_seconds=_positive_number(
            document["candidate_duration_seconds"], "provenance candidate_duration_seconds"
        ),
        source_frames=_positive_integer(document["source_frames"], "provenance source_frames"),
        candidate_frames=_positive_integer(
            document["candidate_frames"], "provenance candidate_frames"
        ),
        crf=_crf_value(document["crf"], "compression provenance crf"),
        encoder=_non_empty_string(document["encoder"], "provenance encoder"),
        preset=_non_empty_string(document["preset"], "provenance preset"),
        generation=generation,
        encoded_at=_non_empty_string(document["encoded_at"], "provenance encoded_at"),
    )


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
    "CompressionSummary",
    "RecordingCompressionCandidate",
    "RecordingCompressionConfiguration",
    "RecordingCompressionError",
    "RecordingCompressionWorker",
    "archive_aged_segments",
    "compress_aged_segments",
    "replace_validated_candidate",
    "validated_candidate",
]
