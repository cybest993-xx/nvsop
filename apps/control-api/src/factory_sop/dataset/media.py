"""媒体探测适配器 seam；最终元数据来自真实内容。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class MediaProbeUnavailableError(Exception):
    """探测工具暂时不可用或超时。"""


class InvalidMediaError(Exception):
    """内容不是含有效视频流的媒体。"""


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    """真实媒体探测出的容器、编码、时长和可选帧采样事实。"""

    duration_seconds: float
    codec: str
    container: str
    fps: float | None = None
    frame_count: int | None = None


class MediaProbe(Protocol):
    """只接受服务端已下载的本地临时文件。"""

    def probe(self, path: str) -> MediaMetadata:
        """探测真实容器、视频流、时长和编码，不访问任意网络来源。"""
        ...
