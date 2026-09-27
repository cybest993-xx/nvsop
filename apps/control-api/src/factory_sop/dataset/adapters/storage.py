"""中心受控本地持久卷上的 dataset 媒体文件适配器。"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from factory_sop.dataset.model import ObjectStat
from factory_sop.dataset.storage import ObjectNotFoundError, ObjectStorageUnavailableError
from factory_sop.settings import Settings

# 未定稿内容先写入该目录；`os.replace` 在同一文件系统内是原子的，因此定稿要么完整
# 可见，要么完全不存在，不会留下半个视频文件被后续 worker 当成真实素材。
_INCOMING_DIRECTORY = ".incoming"
_COPY_CHUNK_BYTES = 1024 * 1024


class LocalFileObjectStorage:
    """把训练素材存放在中心受控本地持久卷；不提供第二套对象存储路径。"""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @classmethod
    def from_settings(cls, settings: Settings) -> LocalFileObjectStorage:
        """从已解析配置创建存储；缺配置时拒绝进入伪本地上传路径。"""
        root = settings.dataset_storage_root
        if root is None or not root.strip():
            raise ObjectStorageUnavailableError("训练素材存储根目录尚未配置")
        return cls(Path(root))

    @contextmanager
    def writing(self, *, object_key: str) -> Iterator[BinaryIO]:
        """流式写入临时文件，正常退出时 fsync 并原子定稿；异常时丢弃。"""
        target = self._path(object_key)
        temporary = self._root / _INCOMING_DIRECTORY / uuid.uuid4().hex
        try:
            temporary.parent.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as sink:
                yield sink
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def stat(self, *, object_key: str) -> ObjectStat:
        """读取已定稿文件的大小；目录或缺失文件都视为不存在。"""
        path = self._path(object_key)
        try:
            info = path.stat()
        except FileNotFoundError as error:
            raise ObjectNotFoundError(object_key) from error
        except OSError as error:
            raise ObjectStorageUnavailableError("无法读取训练素材文件") from error
        if not path.is_file():
            raise ObjectNotFoundError(object_key)
        return ObjectStat(size=info.st_size)

    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        """流式复制定稿文件，不把整段视频放进内存。"""
        path = self._path(object_key)
        try:
            with path.open("rb") as source:
                shutil.copyfileobj(source, destination, length=_COPY_CHUNK_BYTES)
        except FileNotFoundError as error:
            raise ObjectNotFoundError(object_key) from error
        except OSError as error:
            raise ObjectStorageUnavailableError("无法读取训练素材文件") from error

    def delete(self, *, object_key: str) -> None:
        """删除已定稿文件；文件已经不存在时视为成功。"""
        path = self._path(object_key)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            raise ObjectStorageUnavailableError("无法删除训练素材文件") from error

    def _path(self, object_key: str) -> Path:
        """把服务端生成的相对对象键映射到根目录内的绝对路径。"""
        if not object_key or object_key.startswith("/") or ".." in object_key.split("/"):
            raise ObjectStorageUnavailableError("对象键不是安全的相对路径")
        candidate = self._root / object_key
        try:
            candidate.relative_to(self._root)
        except ValueError as error:
            raise ObjectStorageUnavailableError("对象键越出训练素材根目录") from error
        return candidate
