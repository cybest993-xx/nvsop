"""对象存储适配器 seam；业务用例不依赖 MinIO SDK。"""

from __future__ import annotations

from datetime import datetime
from typing import BinaryIO, Protocol

from factory_sop.dataset.model import ObjectStat, UploadInstructions


class ObjectStorageUnavailableError(Exception):
    """对象存储暂时不可用，不能被解释为上传失败或校验成功。"""


class ObjectNotFoundError(Exception):
    """服务端读取不到已分配的对象。"""


class ObjectStorage(Protocol):
    """已分配对象范围内的预签名、事实读取和受限下载接口。"""

    def create_upload(
        self,
        *,
        object_key: str,
        declared_size: int,
        max_bytes: int,
        expires_at: datetime,
    ) -> UploadInstructions:
        """生成只写本次对象的短期上传说明。"""
        ...

    def stat(self, *, object_key: str) -> ObjectStat:
        """读取对象是否存在及实际大小。"""
        ...

    def download_to(
        self,
        *,
        object_key: str,
        destination: BinaryIO,
        version_id: str | None = None,
    ) -> None:
        """把指定对象代次流式写入临时文件，不把整段视频放进请求内存。"""
        ...

    def finalize_upload(
        self,
        *,
        object_key: str,
        source: BinaryIO,
        size: int,
    ) -> ObjectStat:
        """把已验证的临时内容写入服务端定稿对象，隔离仍可能有效的上传地址。"""
        ...

    def delete(self, *, object_key: str) -> None:
        """删除已转为定稿对象的临时上传对象；重复删除必须幂等。"""
        ...
