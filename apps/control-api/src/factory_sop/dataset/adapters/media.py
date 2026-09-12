"""受限本地 ffprobe 媒体探测适配器。"""

from __future__ import annotations

import json
import math
import subprocess
from typing import Any

from factory_sop.dataset.media import InvalidMediaError, MediaMetadata, MediaProbeUnavailableError


class FfprobeMediaProbe:
    """只对服务端临时文件运行 ffprobe，禁止任意网络协议。"""

    def __init__(self, *, binary: str = "ffprobe", timeout_seconds: int = 60) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds

    def probe(self, path: str) -> MediaMetadata:
        """读取容器、首个视频流编码和时长；失败分类不冒充业务成功。"""
        command = [
            self._binary,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,crypto,data",
            "-show_entries",
            "format=format_name,duration:stream=codec_type,codec_name,duration,avg_frame_rate,nb_frames",
            "-of",
            "json",
            path,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout_seconds,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as error:
            raise MediaProbeUnavailableError("ffprobe 不可用或超时") from error
        if completed.returncode != 0:
            raise InvalidMediaError("ffprobe 无法读取媒体")
        try:
            document: Any = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise InvalidMediaError("ffprobe 返回内容无效") from error
        if not isinstance(document, dict):
            raise InvalidMediaError("媒体探测结果结构无效")
        streams = document.get("streams")
        if not isinstance(streams, list):
            raise InvalidMediaError("媒体没有有效视频流")
        video = next(
            (
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "video"
            ),
            None,
        )
        if not isinstance(video, dict) or not isinstance(video.get("codec_name"), str):
            raise InvalidMediaError("媒体没有有效视频流或编码")
        format_info = document.get("format")
        if not isinstance(format_info, dict):
            raise InvalidMediaError("媒体容器信息缺失")
        duration = _number(format_info.get("duration"))
        if duration is None:
            duration = _number(video.get("duration"))
        container = format_info.get("format_name")
        if duration is None or not math.isfinite(duration) or duration <= 0:
            raise InvalidMediaError("媒体时长无效")
        if not isinstance(container, str) or not container.strip():
            raise InvalidMediaError("媒体容器无效")
        fps = _frame_rate(video.get("avg_frame_rate"))
        frame_count = _positive_int(video.get("nb_frames"))
        return MediaMetadata(
            duration_seconds=duration,
            codec=video["codec_name"],
            container=container.split(",", 1)[0],
            fps=fps,
            frame_count=frame_count,
        )


def _frame_rate(value: object) -> float | None:
    if isinstance(value, str) and "/" in value:
        numerator, denominator = value.split("/", 1)
        try:
            numerator_value = float(numerator)
            denominator_value = float(denominator)
        except ValueError:
            return None
        if denominator_value == 0:
            return None
        value = numerator_value / denominator_value
    result = _number(value)
    return result if result is not None and math.isfinite(result) and result > 0 else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None
