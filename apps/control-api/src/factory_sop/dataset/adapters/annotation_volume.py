"""标注基座只读数据卷 adapter。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import BinaryIO

from factory_sop.dataset.annotation import (
    AnnotationDataVolumeInputError,
    AnnotationDataVolumeReadError,
)


class LocalAnnotationDataVolume:
    """在部署提供的只读根目录内读取成功执行的基座文件。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def read_annotation(self, *, data_id: str, video_id: str) -> bytes:
        video_path = self._source_video(data_id=data_id, video_id=video_id)
        annotation_path = video_path.parent / video_path.stem / f"{video_path.stem}_annotation.json"
        return self._read_file(annotation_path)

    def read_video(
        self,
        *,
        data_id: str,
        video_id: str,
        filename: str | None,
        destination: BinaryIO,
    ) -> None:
        video_path = self._source_video(data_id=data_id, video_id=video_id)
        path = (
            video_path
            if filename is None
            else video_path.parent / video_path.stem / _safe_name(filename)
        )
        self._assert_inside_root(path)
        if path.is_symlink() or not path.is_file():
            raise AnnotationDataVolumeInputError("标注基座媒体文件不存在")
        try:
            with path.open("rb") as source:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
        except OSError as error:
            raise AnnotationDataVolumeReadError("标注基座媒体文件不可读取") from error

    def _source_video(self, *, data_id: str, video_id: str) -> Path:
        data_directory = self._root / _safe_name(data_id)
        self._assert_inside_root(data_directory)
        if data_directory.is_symlink() or not data_directory.is_dir():
            raise AnnotationDataVolumeInputError("标注基座数据集目录不存在")
        matches = sorted(data_directory.glob(f"{_safe_name(video_id)}_*.mp4"))
        matches = [path for path in matches if not path.is_symlink() and path.is_file()]
        if len(matches) != 1:
            raise AnnotationDataVolumeInputError("标注基座视频身份映射不唯一")
        self._assert_inside_root(matches[0])
        return matches[0]

    def _read_file(self, path: Path) -> bytes:
        self._assert_inside_root(path)
        if path.is_symlink() or not path.is_file():
            raise AnnotationDataVolumeInputError("标注基座标注文件不存在")
        try:
            return path.read_bytes()
        except OSError as error:
            raise AnnotationDataVolumeReadError("标注基座标注文件不可读取") from error

    def _assert_inside_root(self, path: Path) -> None:
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(self._root)
        except (OSError, ValueError) as error:
            raise AnnotationDataVolumeInputError("标注基座路径越界") from error


def _safe_name(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(character in value for character in "*?[]")
        or os.path.isabs(value)
    ):
        raise AnnotationDataVolumeInputError("标注基座身份不是安全文件名")
    return value
