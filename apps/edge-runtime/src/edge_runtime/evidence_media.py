"""从本机 MediaMTX 滚动录像提取证据片段与锚点关键帧, 原子定稿到本机证据目录 (§5.5/§5.20)。

判定窗口在主机 monotonic 上, 分段名是录像墙钟。入队事务已冻结两者映射与来源相机路径
(`EvidenceSource`), 本模块只按冻结映射定位分段, 绝不读当前时钟重新解释旧 HostInstant,
也不按当前配置给历史判定重绑来源。片段/关键帧/typed 元数据先在临时目录写完再整体原子
rename, 之后才写 SQLite; 失败保持待办并记录具体原因, 且从不删除本机已有媒体。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from time import monotonic, sleep
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from edge_runtime.configuration_values import (
    _non_empty_string,
    _object,
    _positive_number,
    _require_keys,
)
from edge_runtime.judgment.model import HostInstant
from edge_runtime.local_state import LocalState, PendingEvidence, StationStore

MATERIAL_GENERATION_ORIGINAL = "original"
_SEGMENT_TIME_FORMAT = "%Y-%m-%d_%H-%M-%S-%f"
_GAP_TOLERANCE = 0.5
"""分段名时间戳与 ffprobe 时长之间的取整误差容限, 不是可配置的保留时长。"""


class EvidenceMediaError(RuntimeError):
    """一次切片失败的具体原因; 待办保持并记录 ``reason: detail``, 不冒充成功。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class EvidenceMediaConfiguration:
    """本机证据切片的目录、探测二进制、超时与录像时区。"""

    evidence_directory: Path
    ffprobe_binary: Path
    slice_timeout_seconds: float
    recording_timezone: str | None


def load_evidence_media_configuration(value: object) -> EvidenceMediaConfiguration:
    """解析严格的本机证据切片配置; 未知字段直接拒绝。"""
    config = _object(value, "evidence media configuration")
    _require_keys(
        config,
        required={"evidence_directory", "ffprobe_binary", "slice_timeout_seconds"},
        optional={"recording_timezone"},
    )
    timezone_name = config.get("recording_timezone")
    if timezone_name is not None:
        timezone_name = _non_empty_string(timezone_name, "recording_timezone")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"recording_timezone is unknown: {timezone_name}") from error
    return EvidenceMediaConfiguration(
        evidence_directory=_path(config["evidence_directory"], "evidence_directory"),
        ffprobe_binary=_path(config["ffprobe_binary"], "ffprobe_binary"),
        slice_timeout_seconds=_positive_number(
            config["slice_timeout_seconds"], "slice_timeout_seconds"
        ),
        recording_timezone=timezone_name,
    )


@dataclass(frozen=True, slots=True)
class EvidenceClipSet:
    """一次定稿的证据产物: 目录内多来源片段/帧, 加覆盖窗口与可持久化元数据。"""

    evidence_id: str
    directory: Path
    covered_from: float
    covered_to: float
    media_json: str


@dataclass(frozen=True, slots=True)
class _SliceContext:
    host_id: str
    recording_directory: Path
    ffmpeg_binary: Path
    ffprobe_binary: Path
    evidence_directory: Path
    recording_timezone: str | None
    timeout: float


@dataclass(frozen=True, slots=True)
class _Segment:
    path: Path
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def slice_evidence(pending: PendingEvidence, *, context: _SliceContext) -> EvidenceClipSet:
    """按冻结映射从本机分段切片, 原子定稿到证据目录并返回 typed 元数据。

    ``mapping_missing`` 表示入队时没有冻结录像墙钟映射或来源路径 (旧行/无媒体配置), 不能猜成功。
    """
    wall_offset = pending.wall_offset
    if wall_offset is None:
        raise EvidenceMediaError(
            "mapping_missing", "pending evidence has no frozen recording wall clock mapping"
        )
    if not pending.sources:
        raise EvidenceMediaError(
            "mapping_missing", "pending evidence has no frozen recording source"
        )
    wall_from = pending.start.seconds + wall_offset
    wall_to = pending.end.seconds + wall_offset
    anchor_wall = pending.anchor.seconds + wall_offset
    evidence_id = _evidence_id(context.host_id, pending.queue_id)
    context.evidence_directory.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{evidence_id}.", dir=context.evidence_directory))
    try:
        media = [
            _slice_source(
                media_path,
                context=context,
                work=work,
                wall_from=wall_from,
                wall_to=wall_to,
                anchor_wall=anchor_wall,
                wall_offset=wall_offset,
            )
            for media_path in pending.sources
        ]
        covered_from = max(_number(item["actual_from"]) for item in media)
        covered_to = min(_number(item["actual_to"]) for item in media)
        final = context.evidence_directory / _artifact_name(evidence_id, covered_from, covered_to)
        _write_metadata(
            work,
            context.host_id,
            evidence_id,
            pending,
            wall_offset,
            covered_from,
            covered_to,
            media,
        )
        _finalize(work, final)  # 同名完整产物已存在时保留旧权威, 本次内存元数据与它同源
        return EvidenceClipSet(
            evidence_id=evidence_id,
            directory=final,
            covered_from=covered_from,
            covered_to=covered_to,
            media_json=json.dumps(media, separators=(",", ":")),
        )
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _slice_source(
    media_path: str,
    *,
    context: _SliceContext,
    work: Path,
    wall_from: float,
    wall_to: float,
    anchor_wall: float,
    wall_offset: float,
) -> dict[str, object]:
    directory = context.recording_directory / media_path
    if not directory.is_dir():
        raise EvidenceMediaError("material_missing", f"recording directory absent for {media_path}")
    selected = _select_segments(
        _list_segments(
            directory, context.recording_timezone, context.ffprobe_binary, context.timeout
        ),
        wall_from,
        wall_to,
    )
    actual_from, actual_to = selected[0].start, selected[-1].end
    clip = work / f"{media_path}.mp4"
    _concat(context.ffmpeg_binary, selected, clip, context.timeout, work)
    _extract_keyframe(
        context.ffmpeg_binary,
        clip,
        max(0.0, anchor_wall - actual_from),
        work / f"{media_path}.jpg",
        context.timeout,
    )
    _fsync(clip)
    return {
        "media_path": media_path,
        "clip_file": clip.name,
        "keyframe_file": f"{media_path}.jpg",
        "actual_from": actual_from - wall_offset,
        "actual_to": actual_to - wall_offset,
        "material_generation": MATERIAL_GENERATION_ORIGINAL,
        "sha256": _digest(clip),
        "size_bytes": clip.stat().st_size,
    }


def _list_segments(
    directory: Path, recording_timezone: str | None, ffprobe_binary: Path, timeout: float
) -> list[_Segment]:
    segments: list[_Segment] = []
    for path in sorted(directory.glob("*.mp4")):
        start = _segment_start(path.name, recording_timezone)
        duration = None if start is None else _probe_duration(ffprobe_binary, path, timeout)
        if start is not None and duration is not None:
            # 正在写入的最新分段或损坏分段跳过, 让覆盖检查如实报告素材缺口。
            segments.append(_Segment(path=path, start=start, duration=duration))
    segments.sort(key=lambda segment: segment.start)
    return segments


def _select_segments(segments: list[_Segment], wall_from: float, wall_to: float) -> list[_Segment]:
    selected = [s for s in segments if s.start < wall_to and s.end > wall_from]
    if not selected:
        raise EvidenceMediaError("material_missing", "no recording segment overlaps the window")
    if selected[0].start > wall_from:
        raise EvidenceMediaError("material_missing", "recording starts after the window begins")
    if selected[-1].end < wall_to:
        raise EvidenceMediaError("material_missing", "recording ends before the window ends")
    previous = selected[0]
    for segment in selected[1:]:
        if segment.start > previous.end + _GAP_TOLERANCE:
            raise EvidenceMediaError("material_missing", "recording has a gap inside the window")
        previous = segment
    return selected


def _concat(
    ffmpeg_binary: Path, segments: list[_Segment], destination: Path, timeout: float, work: Path
) -> None:
    listing = work / "segments.txt"
    listing.write_text(
        "".join(f"file '{segment.path.as_posix()}'\n" for segment in segments), encoding="utf-8"
    )
    _run(
        [
            str(ffmpeg_binary),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(destination),
        ],
        timeout,
    )
    listing.unlink(missing_ok=True)


def _extract_keyframe(
    ffmpeg_binary: Path, clip: Path, offset: float, destination: Path, timeout: float
) -> None:
    _run(
        [
            str(ffmpeg_binary),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{offset:.3f}",
            "-i",
            str(clip),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-y",
            str(destination),
        ],
        timeout,
    )


def _write_metadata(
    work: Path,
    host_id: str,
    evidence_id: str,
    pending: PendingEvidence,
    wall_offset: float,
    covered_from: float,
    covered_to: float,
    media: list[dict[str, object]],
) -> None:
    metadata = {
        "evidence_id": evidence_id,
        "host_id": host_id,
        "anchor": pending.anchor.seconds,
        "window_from": pending.start.seconds,
        "window_to": pending.end.seconds,
        "wall_offset": wall_offset,
        "covered_from": covered_from,
        "covered_to": covered_to,
        "media": media,
    }
    path = work / "metadata.json"
    path.write_text(json.dumps(metadata, separators=(",", ":")) + "\n", encoding="utf-8")
    _fsync(path)


def _finalize(work: Path, final: Path) -> None:
    """整体原子 rename; 目标已存在完整产物时保留旧权威, 由调用方复用其元数据。"""
    _fsync(work)
    try:
        os.rename(work, final)
    except OSError:
        if not final.is_dir():
            raise EvidenceMediaError("finalize_failed", f"cannot finalize {final.name}") from None
        shutil.rmtree(work, ignore_errors=True)


def _run(argv: list[str], timeout: float) -> None:
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise EvidenceMediaError(
            "ffmpeg_failed", f"command timed out after {timeout:g}s"
        ) from error
    except OSError as error:
        raise EvidenceMediaError("ffmpeg_failed", str(error)) from error
    if completed.returncode != 0:
        lines = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise EvidenceMediaError(
            "ffmpeg_failed", f"exit {completed.returncode}: {lines[-1] if lines else 'no detail'}"
        )


def _probe_duration(ffprobe_binary: Path, path: Path, timeout: float) -> float | None:
    """读取分段真实时长; 不可解码/正在写入返回 None, 由覆盖检查判定素材缺口。"""
    try:
        completed = subprocess.run(
            [
                str(ffprobe_binary),
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
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError as error:
        raise EvidenceMediaError("ffmpeg_failed", str(error)) from error
    if completed.returncode != 0:
        return None
    try:
        payload = cast(dict[str, object], json.loads(completed.stdout))
        streams = payload["streams"]
        duration = _number(cast(dict[str, object], payload["format"])["duration"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    has_video = isinstance(streams, list) and any(
        isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams
    )
    return duration if has_video and duration > 0 else None


def _segment_start(name: str, recording_timezone: str | None) -> float | None:
    stem = name[:-4] if name.endswith(".mp4") else name
    try:
        parsed = datetime.strptime(stem, _SEGMENT_TIME_FORMAT)
    except ValueError:
        return None
    if recording_timezone is None:
        # MediaMTX 与本机 Python 进程同一本机时区; 部署须保证或显式配置时区。
        return parsed.timestamp()
    return parsed.replace(tzinfo=ZoneInfo(recording_timezone)).timestamp()


def _evidence_id(host_id: str, queue_id: int) -> str:
    """稳定证据身份复用 host + queue_id (队列主键), 不 hash 浮点锚点新造身份。"""
    return f"{re.sub(r'[^A-Za-z0-9._-]', '_', host_id)}-{queue_id}"


def _artifact_name(evidence_id: str, covered_from: float, covered_to: float) -> str:
    """产物目录名编码实际覆盖窗口, 使扩窗重切追加新目录而不覆盖旧权威。"""
    return f"{evidence_id}-{round(covered_from * 1000)}-{round(covered_to * 1000)}"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _number(value: object) -> float:
    # ffprobe 的 format.duration 是字符串, 我们写入的元数据是 JSON 数字; 两者都接受。
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("value must be a number")
    result = float(value)
    if not isfinite(result):
        raise ValueError("value must be finite")
    return result


def _path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty path string")
    return Path(value)


class EvidenceMediaWorker:
    """后台消费本机证据待办: 不阻塞判定, 不依赖中心, 不删除本机媒体 (S033)。"""

    def __init__(
        self,
        *,
        state: LocalState,
        host_id: str,
        recording_directory: Path,
        ffmpeg_binary: Path,
        configuration: EvidenceMediaConfiguration,
        interval: float,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if interval <= 0:
            raise ValueError("evidence media interval must be positive")
        self._state = state
        self._context = _SliceContext(
            host_id=host_id,
            recording_directory=recording_directory,
            ffmpeg_binary=ffmpeg_binary,
            ffprobe_binary=configuration.ffprobe_binary,
            evidence_directory=configuration.evidence_directory,
            recording_timezone=configuration.recording_timezone,
            timeout=configuration.slice_timeout_seconds,
        )
        self._interval = interval
        self._clock = clock

    def drain(self) -> int:
        """处理一轮全部待切片证据, 返回本轮成功定稿数; 跨工位读取, 移除工位不丢待办。"""
        produced = 0
        for station_id in self._state.pending_evidence_stations():
            station = self._state.station(station_id)
            for pending in station.evidence_awaiting_slice():
                if self._slice(station, pending):
                    produced += 1
        return produced

    def run_forever(self, *, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            self.drain()
            deadline = monotonic() + self._interval
            while not should_stop() and monotonic() < deadline:
                sleep(0.05)

    def _slice(self, station: StationStore, pending: PendingEvidence) -> bool:
        now = HostInstant(self._clock())
        try:
            clip_set = slice_evidence(pending, context=self._context)
        except EvidenceMediaError as error:
            station.record_evidence_failure(
                pending.queue_id, at=now, error=f"{error.reason}: {error.detail}"
            )
            return False
        station.record_evidence_slice(
            pending.queue_id,
            at=now,
            media_results=clip_set.media_json,
            covered_from=clip_set.covered_from,
            covered_to=clip_set.covered_to,
        )
        return True
