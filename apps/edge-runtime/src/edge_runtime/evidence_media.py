"""从本机 MediaMTX 滚动录像提取证据片段与锚点关键帧, 原子定稿到本机证据目录 (§5.5/§5.20)。

入队事务已冻结判定窗口与录像墙钟的映射及来源相机路径 (`EvidenceSource`), 本模块只按冻结映射
定位分段, 绝不读当前时钟重新解释旧 HostInstant, 也不按当前配置给历史判定重绑来源。片段/关键帧/
typed 元数据先在临时目录写完再整体原子 rename, 之后才写 SQLite; 任一步失败都在此处翻译为具体原因
并保持待办, 从不删除本机已有媒体。标准库 + 本机 ffmpeg/ffprobe。
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
_BOUNDARY_TOLERANCE = 1e-3
"""相邻分段边界抖动容限。实测多数真实 MediaMTX fmp4 边界抖动 ≤11µs, 但首段可与次段重叠约 0.5s;
重叠无法证明时间对应, 不得静默 concat 重复内容, 见 `_select_segments`。"""
_FFMPEG_HEAD = ("-hide_banner", "-loglevel", "error")
_FFPROBE_ARGS = ("-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json")


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
        _path(config["evidence_directory"], "evidence_directory"),
        _path(config["ffprobe_binary"], "ffprobe_binary"),
        _positive_number(config["slice_timeout_seconds"], "slice_timeout_seconds"),
        timezone_name,
    )


@dataclass(frozen=True, slots=True)
class EvidenceClipSet:
    """一次定稿产物的覆盖窗口 (monotonic) 与每来源 typed 元数据 JSON。"""

    covered_from: float
    covered_to: float
    media_json: str


@dataclass(frozen=True, slots=True)
class _Segment:
    path: Path
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration


def slice_evidence(
    pending: PendingEvidence,
    *,
    host_id: str,
    recording_directory: Path,
    ffmpeg_binary: Path,
    configuration: EvidenceMediaConfiguration,
) -> EvidenceClipSet:
    """按冻结映射从本机分段切片, 原子定稿到证据目录并返回 typed 元数据。

    ``mapping_missing`` 表示入队时没有冻结录像墙钟映射或来源路径 (旧行/无媒体配置), 不能猜成功。
    """
    wall_offset = pending.wall_offset
    if wall_offset is None or not pending.sources:
        raise EvidenceMediaError(
            "mapping_missing", "pending evidence has no frozen recording source mapping"
        )
    evidence_id = _evidence_id(host_id, pending.queue_id)
    try:
        configuration.evidence_directory.mkdir(parents=True, exist_ok=True)
        work = Path(
            tempfile.mkdtemp(prefix=f".{evidence_id}.", dir=configuration.evidence_directory)
        )
    except OSError as error:
        raise EvidenceMediaError("media_io_failed", str(error)) from error
    try:
        try:
            media = [
                _slice_source(
                    source,
                    pending=pending,
                    configuration=configuration,
                    recording_directory=recording_directory,
                    ffmpeg_binary=ffmpeg_binary,
                    work=work,
                    wall_offset=wall_offset,
                )
                for source in pending.sources
            ]
        except OSError as error:
            raise EvidenceMediaError("media_io_failed", str(error)) from error
        covered_from = max(_number(item["actual_from"]) for item in media)
        covered_to = min(_number(item["actual_to"]) for item in media)
        final = configuration.evidence_directory / _artifact_name(
            evidence_id, covered_from, covered_to
        )
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
        try:
            metadata_path = work / "metadata.json"
            metadata_path.write_text(
                json.dumps(metadata, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            _fsync(metadata_path)
            _fsync(work)
            published = _publish(work, final)
        except OSError as error:
            raise EvidenceMediaError("finalize_failed", str(error)) from error
        if not published:  # 目标已有完整产物: 复用旧权威 metadata, 不用本次新 hash/size 伪报
            covered_from, covered_to, media = _read_metadata(final)
        return EvidenceClipSet(
            covered_from=covered_from,
            covered_to=covered_to,
            media_json=json.dumps(media, separators=(",", ":")),
        )
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise


def _slice_source(
    source: str,
    *,
    pending: PendingEvidence,
    configuration: EvidenceMediaConfiguration,
    recording_directory: Path,
    ffmpeg_binary: Path,
    work: Path,
    wall_offset: float,
) -> dict[str, object]:
    wall_from = pending.start.seconds + wall_offset
    wall_to = pending.end.seconds + wall_offset
    directory = recording_directory / source
    if not directory.is_dir():
        raise EvidenceMediaError("material_missing", f"recording directory absent for {source}")
    selected = _select_segments(_list_segments(directory, configuration), wall_from, wall_to)
    actual_from, actual_to = selected[0].start, selected[-1].end
    clip = work / f"{source}.mp4"
    timeout = configuration.slice_timeout_seconds
    inputs = ["-f", "concat", "-safe", "0", "-i", str(_segment_list(work, selected))]
    outputs = ["-c", "copy", "-movflags", "+faststart", "-y", str(clip)]
    _ffmpeg([str(ffmpeg_binary), *_FFMPEG_HEAD, *inputs, *outputs], timeout)
    keyframe = work / f"{source}.jpg"
    anchor_offset = max(0.0, pending.anchor.seconds + wall_offset - actual_from)
    seek = ["-ss", f"{anchor_offset:.3f}", "-i", str(clip)]
    still = ["-frames:v", "1", "-q:v", "2", "-y", str(keyframe)]
    _ffmpeg([str(ffmpeg_binary), *_FFMPEG_HEAD, *seek, *still], timeout)
    if not keyframe.is_file() or keyframe.stat().st_size == 0:
        raise EvidenceMediaError("ffmpeg_failed", "anchor keyframe was not produced")
    _fsync(clip)
    return {
        "media_path": source,
        "clip_file": clip.name,
        "keyframe_file": keyframe.name,
        "actual_from": actual_from - wall_offset,
        "actual_to": actual_to - wall_offset,
        "material_generation": MATERIAL_GENERATION_ORIGINAL,
        "sha256": _digest(clip),
        "size_bytes": clip.stat().st_size,
    }


def _segment_list(work: Path, segments: list[_Segment]) -> Path:
    listing = work / "segments.txt"
    listing.write_text(
        "".join(f"file '{segment.path.as_posix()}'\n" for segment in segments), encoding="utf-8"
    )
    return listing


def _list_segments(directory: Path, configuration: EvidenceMediaConfiguration) -> list[_Segment]:
    segments: list[_Segment] = []
    for path in sorted(directory.glob("*.mp4")):
        start = _segment_start(path.name, configuration.recording_timezone)
        duration = (
            None
            if start is None
            else _probe_duration(
                configuration.ffprobe_binary, path, configuration.slice_timeout_seconds
            )
        )
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
    chosen = [selected[0]]
    covered = selected[0].end
    for segment in selected[1:]:
        if segment.start > covered + _BOUNDARY_TOLERANCE:
            raise EvidenceMediaError("material_missing", "recording has a gap inside the window")
        if covered - segment.start > _BOUNDARY_TOLERANCE:
            # 大重叠无法证明两段在时间上对应, concat 会重复内容并错位锚点; 窗口不依赖旧分段
            # (它从本段起已被覆盖) 时丢弃旧分段, 否则显式失败保持待办。
            if wall_from < segment.start:
                raise EvidenceMediaError(
                    "material_overlap", "adjacent recording segments overlap beyond tolerance"
                )
            chosen, covered = [segment], segment.end
            continue
        covered = max(covered, segment.end)
        chosen.append(segment)
    if covered < wall_to:
        raise EvidenceMediaError("material_missing", "recording ends before the window ends")
    return chosen


def _publish(work: Path, final: Path) -> bool:
    """整体原子 rename; 目标已存在完整产物时保留旧权威并返回 False。"""
    try:
        os.rename(work, final)
    except OSError:
        if not final.is_dir():
            raise EvidenceMediaError("finalize_failed", f"cannot finalize {final.name}") from None
        shutil.rmtree(work, ignore_errors=True)
        return False
    return True


def _read_metadata(directory: Path) -> tuple[float, float, list[dict[str, object]]]:
    try:
        metadata = _object(
            json.loads((directory / "metadata.json").read_text(encoding="utf-8")),
            "evidence metadata",
        )
        media = [cast(dict[str, object], item) for item in cast(list[object], metadata["media"])]
        complete = bool(media) and all(
            (directory / cast(str, item["clip_file"])).is_file()
            and (directory / cast(str, item["keyframe_file"])).is_file()
            for item in media
        )
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise EvidenceMediaError(
            "finalize_failed", f"existing artifact unreadable: {error}"
        ) from error
    if not complete:
        raise EvidenceMediaError(
            "finalize_failed", f"existing artifact {directory.name} incomplete"
        )
    return _number(metadata["covered_from"]), _number(metadata["covered_to"]), media


def _spawn(argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as error:
        raise EvidenceMediaError(
            "ffmpeg_failed", f"command timed out after {timeout:g}s"
        ) from error
    except OSError as error:
        raise EvidenceMediaError("ffmpeg_failed", str(error)) from error


def _ffmpeg(argv: list[str], timeout: float) -> None:
    completed = _spawn(argv, timeout)
    if completed.returncode != 0:
        lines = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise EvidenceMediaError(
            "ffmpeg_failed", f"exit {completed.returncode}: {lines[-1] if lines else 'no detail'}"
        )


def _probe_duration(ffprobe_binary: Path, path: Path, timeout: float) -> float | None:
    """读取分段真实时长; 不可解码/正在写入返回 None, 由覆盖检查判定素材缺口。"""
    completed = _spawn([str(ffprobe_binary), *_FFPROBE_ARGS, str(path)], timeout)
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


def evidence_registration_payloads(
    pending: PendingEvidence, *, host_id: str, station_id: str, directory: Path
) -> tuple[dict[str, object], ...]:
    """从已定稿证据构造不可变中心引用; 扩窗以新产物目录形成新证据身份。"""
    if pending.media_results is None or pending.covered_from is None or pending.covered_to is None:
        raise ValueError("evidence media has not been finalized")
    if pending.covered_from > pending.start.seconds or pending.covered_to < pending.end.seconds:
        raise ValueError("evidence material does not cover the requested window")
    artifact = _artifact_name(
        _evidence_id(host_id, pending.queue_id), pending.covered_from, pending.covered_to
    )
    media = json.loads(pending.media_results)
    results: list[dict[str, object]] = []
    for item in media:
        for kind, field in (("clip", "clip_file"), ("keyframe", "keyframe_file")):
            name = item[field]
            if not isinstance(name, str) or "/" in name or "\\" in name or name in {".", ".."}:
                raise ValueError("evidence metadata contains an invalid filename")
            file_path = directory / artifact / name
            size = file_path.stat().st_size
            digest = _digest(file_path)
            if kind == "clip" and (digest != item["sha256"] or size != item["size_bytes"]):
                raise ValueError("evidence media does not match its finalized digest")
            reference = f"{artifact}/{name}"
            results.append(
                {
                    "evidence_id": reference,
                    "host_id": host_id,
                    "station_id": station_id,
                    "instance_id": pending.instance_id,
                    "violation_id": None,
                    "kind": kind,
                    "origin": "automatic",
                    "anchor": pending.anchor.seconds,
                    "window_start": pending.start.seconds,
                    "window_end": pending.end.seconds,
                    "generation": item["material_generation"],
                    "sha256": digest,
                    "size": size,
                    "reference": reference,
                }
            )
    return tuple(results)


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
        self._host_id = host_id
        self._recording_directory = recording_directory
        self._ffmpeg_binary = ffmpeg_binary
        self._configuration = configuration
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
            clip_set = slice_evidence(
                pending,
                host_id=self._host_id,
                recording_directory=self._recording_directory,
                ffmpeg_binary=self._ffmpeg_binary,
                configuration=self._configuration,
            )
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
