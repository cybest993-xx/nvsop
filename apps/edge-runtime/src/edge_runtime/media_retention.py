"""本机录像窗口缩短时的实际文件影响估算与确认。"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from time import time
from typing import cast


@dataclass(frozen=True, slots=True)
class RecordingImpact:
    """一份基于当前录像目录的窗口缩短影响快照。"""

    previous_window_seconds: int
    target_window_seconds: int
    segment_count: int
    bytes: int
    oldest_segment_at: str | None
    newest_segment_at: str | None
    generated_at: str
    confirmed_by: str | None = None

    def to_wire(self) -> dict[str, object]:
        """转换为严格的本地配置字段。"""
        return {
            "previous_window_seconds": self.previous_window_seconds,
            "target_window_seconds": self.target_window_seconds,
            "segment_count": self.segment_count,
            "bytes": self.bytes,
            "oldest_segment_at": self.oldest_segment_at,
            "newest_segment_at": self.newest_segment_at,
            "generated_at": self.generated_at,
            "confirmed_by": self.confirmed_by,
        }


def applied_window_path(media_config_path: Path) -> Path:
    """返回成功应用配置的旁车记录路径."""
    return media_config_path.with_name(f"{media_config_path.name}.applied")


def read_applied_window(media_config_path: Path) -> int | None:
    """读取最近一次成功应用的窗口; 缺少旁车记录表示没有可比较事实。"""
    path = applied_window_path(media_config_path)
    if not path.exists():
        return None
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or set(raw) != {"recording_window_seconds"}:
        raise ValueError("applied media state has unsupported fields")
    value = cast(dict[str, object], raw)
    window = value["recording_window_seconds"]
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("applied media state has an invalid recording window")
    return window


def write_applied_window(media_config_path: Path, recording_window_seconds: int) -> None:
    """在媒体进程成功启动后原子记录本次有效窗口。"""
    if recording_window_seconds <= 0:
        raise ValueError("recording window must be positive")
    path = applied_window_path(media_config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(
                {"recording_window_seconds": recording_window_seconds},
                stream,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def estimate_recording_impact(
    directory: Path,
    *,
    previous_window_seconds: int,
    target_window_seconds: int,
    now: float | None = None,
    confirmed_by: str | None = None,
) -> RecordingImpact:
    """扫描真实分段文件, 计算缩短窗口会进入后续清理范围的对象。"""
    if previous_window_seconds <= target_window_seconds:
        raise ValueError("recording impact is only required when shortening the window")
    if not directory.is_dir():
        raise FileNotFoundError(f"recording directory does not exist: {directory}")
    current_time = time() if now is None else now
    if not isfinite(current_time):
        raise ValueError("impact timestamp must be finite")
    previous_cutoff = current_time - previous_window_seconds
    cutoff = current_time - target_window_seconds
    affected: list[tuple[float, int]] = []
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        if previous_cutoff <= stat.st_mtime < cutoff:
            affected.append((stat.st_mtime, stat.st_size))
    affected.sort()
    return RecordingImpact(
        previous_window_seconds=previous_window_seconds,
        target_window_seconds=target_window_seconds,
        segment_count=len(affected),
        bytes=sum(size for _, size in affected),
        oldest_segment_at=_utc(affected[0][0]) if affected else None,
        newest_segment_at=_utc(affected[-1][0]) if affected else None,
        generated_at=_utc(current_time),
        confirmed_by=confirmed_by,
    )


def parse_recording_impact(value: object) -> RecordingImpact:
    """读取严格的人工确认快照; 未知字段不静默忽略。"""
    if not isinstance(value, dict):
        raise ValueError("recording_window_confirmation must be an object")
    required = {
        "previous_window_seconds",
        "target_window_seconds",
        "segment_count",
        "bytes",
        "oldest_segment_at",
        "newest_segment_at",
        "generated_at",
        "confirmed_by",
    }
    if set(value) != required:
        raise ValueError("recording_window_confirmation has unsupported fields")
    integer_fields = (
        "previous_window_seconds",
        "target_window_seconds",
        "segment_count",
        "bytes",
    )
    integers: dict[str, int] = {}
    for field in integer_fields:
        item = value[field]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"recording_window_confirmation.{field} must be non-negative")
        integers[field] = item
    if integers["previous_window_seconds"] <= integers["target_window_seconds"]:
        raise ValueError("recording_window_confirmation must confirm a shortened window")
    timestamp_fields: dict[str, str | None] = {}
    for field in ("oldest_segment_at", "newest_segment_at"):
        item = value[field]
        if item is not None and not isinstance(item, str):
            raise ValueError(f"recording_window_confirmation.{field} must be a string or null")
        timestamp_fields[field] = item
    generated_at = value["generated_at"]
    if not isinstance(generated_at, str) or not generated_at:
        raise ValueError("recording_window_confirmation.generated_at must be a string")
    confirmed_by = value["confirmed_by"]
    if confirmed_by is not None and (not isinstance(confirmed_by, str) or not confirmed_by.strip()):
        raise ValueError("recording_window_confirmation.confirmed_by must be a string or null")
    return RecordingImpact(
        **integers,
        oldest_segment_at=timestamp_fields["oldest_segment_at"],
        newest_segment_at=timestamp_fields["newest_segment_at"],
        generated_at=generated_at,
        confirmed_by=confirmed_by,
    )


def same_impact(left: RecordingImpact, right: RecordingImpact) -> bool:
    """比较会影响删除范围的事实, 忽略两次扫描的生成时刻。"""
    return (
        left.previous_window_seconds == right.previous_window_seconds
        and left.target_window_seconds == right.target_window_seconds
        and left.segment_count == right.segment_count
        and left.bytes == right.bytes
        and left.oldest_segment_at == right.oldest_segment_at
        and left.newest_segment_at == right.newest_segment_at
    )


def _utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "RecordingImpact",
    "applied_window_path",
    "estimate_recording_impact",
    "parse_recording_impact",
    "read_applied_window",
    "same_impact",
    "write_applied_window",
]
