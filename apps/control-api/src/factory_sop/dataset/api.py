"""`dataset` 对 HTTP 和 job adapter 暴露的最小跨模块契约。

公开入口分三类：job adapter 用 `DatasetResourceLookup` 验证任务归属；校验 worker
用 `DatasetValidationRuntime` 装配基础设施并完成视频校验；标注 worker 用
`DatasetAnnotationRuntime` 装配基座、对象存储和媒体探测器，并推进上下文准备与切片执行。
视频成员、仓储和上传存储类型留在 `dataset` 自己的模块边界内，不通过这里扩散。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from factory_sop.dataset.annotation import (
    AnnotationBackend,
    AnnotationBackendExecutionError,
    AnnotationBackendUnavailableError,
)
from factory_sop.dataset.errors import DatasetRefusedError
from factory_sop.dataset.media import MediaProbe
from factory_sop.dataset.model import MemberStatus
from factory_sop.dataset.repository import DatasetRepository
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.dataset.usecases import begin_video_validation, validate_video_upload
from factory_sop.dataset.usecases.annotation import (
    AnnotationContextPreparationTarget,
    AnnotationExecutionTarget,
    PreparedAnnotationCopy,
    begin_annotation_context_preparation,
    begin_annotation_execution,
    complete_annotation_context_preparation,
    complete_annotation_execution,
    fail_annotation_context_preparation,
    fail_annotation_execution,
    prepare_annotation_context_copy,
    prepare_annotation_execution_copy,
    save_annotation_execution_copy,
)


class DatasetResourceLookup(Protocol):
    """供 `job` 确认成员和上传尝试归属的最小查询契约。"""

    def resource_exists(self, member_id: UUID, attempt_id: UUID) -> bool:
        """确认成员与尝试存在且尝试属于该成员。"""
        ...


class DatasetAnnotationRuntime(Protocol):
    """供 `job` worker 使用的真实标注资源工厂契约。"""

    def repository(self, session: object) -> DatasetRepository:
        """为一个短事务创建数据集仓储。"""
        ...

    def storage(self) -> ObjectStorage:
        """创建对象存储客户端，调用发生在数据库事务外。"""
        ...

    def backend(self) -> AnnotationBackend:
        """创建复用标注基座的 HTTP adapter。"""
        ...

    def media_probe(self) -> MediaProbe:
        """创建转码后派生视频的媒体事实探测器。"""
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
    "AnnotationBackendExecutionError",
    "AnnotationBackendUnavailableError",
    "AnnotationContextPreparationTarget",
    "AnnotationExecutionTarget",
    "DatasetAnnotationRuntime",
    "DatasetRefusedError",
    "DatasetResourceLookup",
    "DatasetValidationRuntime",
    "MemberStatus",
    "PreparedAnnotationCopy",
    "begin_annotation_context_preparation",
    "begin_annotation_execution",
    "begin_video_validation",
    "complete_annotation_context_preparation",
    "complete_annotation_execution",
    "fail_annotation_context_preparation",
    "fail_annotation_execution",
    "prepare_annotation_context_copy",
    "prepare_annotation_execution_copy",
    "save_annotation_execution_copy",
    "validate_video_upload",
]
