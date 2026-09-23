"""`dataset` 标注兼容适配器使用的最小接口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

from factory_sop.dataset.model import AnnotationMode, AnnotationSegment


@dataclass(frozen=True, slots=True)
class PreparedAnnotationVideo:
    """基座工作区中的视频身份；不向浏览器暴露。"""

    data_id: str
    video_id: str


class AnnotationBackendUnavailableError(Exception):
    """基座兼容服务或其连接暂时不可用。"""


class AnnotationBackendExecutionError(Exception):
    """基座已接受请求但切片执行失败。"""


class AnnotationDataVolumeUnavailableError(Exception):
    """基座标注数据卷不可读取或映射不安全。"""


class AnnotationDataVolumeInputError(AnnotationDataVolumeUnavailableError):
    """成功执行身份、映射或只读文件不存在，需修正输入或部署映射。"""


class AnnotationDataVolumeReadError(AnnotationDataVolumeUnavailableError):
    """只读数据卷暂时无法读取，重试可能恢复。"""


class AnnotationDataVolume(Protocol):
    """只读读取标注基座已经成功写出的数据。"""

    def read_annotation(self, *, data_id: str, video_id: str) -> bytes:
        """读取指定成功执行对应的视频标注 JSON。"""
        ...

    def read_video(
        self,
        *,
        data_id: str,
        video_id: str,
        filename: str | None,
        destination: BinaryIO,
    ) -> None:
        """读取完整视频或指定成功执行切片到临时文件。"""
        ...


class AnnotationBackend(Protocol):
    """复用的标注基座所需的窄适配器 seam。"""

    def prepare_video(
        self,
        *,
        source: BinaryIO,
        filename: str,
        actions: Sequence[str],
    ) -> PreparedAnnotationVideo:
        """把已确认源视频和一份动作清单放入基座工作区。"""
        ...

    def discard_prepared_video(self, *, data_id: str) -> None:
        """删除尚未持久化引用的基座工作副本。"""
        ...

    def download_video(self, *, video_id: str, destination: BinaryIO) -> None:
        """把基座转码后的派生视频流入服务端临时文件。"""
        ...

    def split_video(
        self,
        *,
        video_id: str,
        segments: Sequence[AnnotationSegment],
        mode: AnnotationMode,
    ) -> Sequence[Mapping[str, Any]]:
        """调用基座切片，并返回原始 clip 结果供候选记录保存。"""
        ...
