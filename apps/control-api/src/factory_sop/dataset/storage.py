"""数据集媒体存储 seam；业务用例不依赖具体文件系统实现。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import BinaryIO, Protocol

from factory_sop.dataset.model import ObjectStat


class ObjectStorageUnavailableError(Exception):
    """媒体存储暂时不可用，不能被解释为上传失败或校验成功。"""


class ObjectNotFoundError(Exception):
    """服务端读取不到已定稿的媒体文件。"""


class ObjectStorage(Protocol):
    """dataset 拥有对象的定稿写入、事实读取和受限下载接口。"""

    def writing(self, *, object_key: str) -> AbstractContextManager[BinaryIO]:
        """打开一个临时写入目标；正常退出时原子定稿，异常时丢弃且不留定稿文件。"""
        ...

    def stat(self, *, object_key: str) -> ObjectStat:
        """读取已定稿对象是否存在及实际大小。"""
        ...

    def download_to(self, *, object_key: str, destination: BinaryIO) -> None:
        """把指定对象流式写入目标，不把整段视频放进请求内存。"""
        ...

    def delete(self, *, object_key: str) -> None:
        """删除对象；重复删除必须幂等。"""
        ...
