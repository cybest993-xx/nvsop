"""`dataset` 对 HTTP job route 和校验 worker 暴露的最小跨模块契约。

公开入口只有两类：job adapter 用 `DatasetResourceLookup` 验证任务归属；校验 worker
用 `DatasetValidationRuntime` 装配基础设施，并调用 `begin_video_validation` 和
`validate_video_upload` 完成一次校验。`MemberStatus` 只用于 worker 判定校验终态。
视频成员、仓储和上传存储类型留在 `dataset` 自己的模块边界内，不通过这里扩散。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.dataset.media import MediaProbe
from factory_sop.dataset.model import MemberStatus
from factory_sop.dataset.repository import DatasetRepository
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases import begin_video_validation, validate_video_upload


class DatasetResourceLookup(Protocol):
    """供 `job` 确认成员和上传尝试归属的最小查询契约。"""

    def resource_exists(self, member_id: UUID, attempt_id: UUID) -> bool:
        """确认成员与尝试存在且尝试属于该成员。"""
        ...


class DatasetValidationRuntime(Protocol):
    """供 `job` worker 使用的真实数据集资源工厂契约。"""

    def repository(self, session: object) -> DatasetRepository:
        """为一个短事务创建数据集仓储。"""
        ...

    def storage(self) -> ObjectStorage:
        """创建对象存储客户端，调用发生在数据库事务外。"""
        ...

    def media_probe(self) -> MediaProbe:
        """创建媒体探测器，调用发生在数据库事务外。"""
        ...

    def supported_codecs(self) -> frozenset[str]:
        """返回部署允许的媒体编码集合。"""
        ...


__all__ = [
    "DatasetResourceLookup",
    "DatasetValidationRuntime",
    "MemberStatus",
    "begin_video_validation",
    "validate_video_upload",
]
