"""`dataset` 对 HTTP 和 job adapter 暴露的最小跨模块契约。

公开入口分三类：job adapter 用 `DatasetResourceLookup` 验证任务归属；校验 worker
用 `DatasetValidationRuntime` 装配基础设施并完成视频校验；标注 worker 用
`DatasetAnnotationRuntime` 装配基座、对象存储和媒体探测器，并推进上下文准备与切片执行。
视频成员、仓储和上传存储类型留在 `dataset` 自己的模块边界内，不通过这里扩散。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from factory_sop.auth.api import Caller
from factory_sop.dataset.annotation import (
    AnnotationBackend,
    AnnotationBackendExecutionError,
    AnnotationBackendUnavailableError,
    AnnotationDataVolume,
    AnnotationDataVolumeUnavailableError,
)
from factory_sop.dataset.errors import DatasetRefusalCode, DatasetRefusedError
from factory_sop.dataset.media import MediaProbe, MediaProbeUnavailableError
from factory_sop.dataset.model import (
    ArtifactStatus,
    MemberStatus,
    UsageCheckStatus,
    UsageKind,
)
from factory_sop.dataset.repository import DatasetRepository, UsageDatasetRepository
from factory_sop.dataset.storage import ObjectStorage, ObjectStorageUnavailableError
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
from factory_sop.dataset.usecases.usage import (
    ArtifactTarget,
    UsageCheckTarget,
    apply_usage_check_currentness,
    begin_artifact_generation,
    begin_usage_check,
    complete_artifact,
    complete_artifact_candidate_cleanup,
    complete_usage_check,
    fail_artifact,
    fail_usage_check,
    invalidate_artifact_for_job,
    mark_artifact_cleanup_pending,
    record_artifact_orphan_candidate,
    render_ddm_artifact_with_base,
    run_usage_check,
)


def summary(*, caller: Caller, datasets: DatasetRepository) -> dict[str, object]:
    """返回 overview 使用的权限裁剪数据集摘要。"""
    from factory_sop.dataset.usecases.summary import summary as build_summary

    return build_summary(caller=caller, datasets=datasets)


@dataclass(frozen=True, slots=True)
class CheckedDatasetInput:
    """供后续训练模块消费的一次已通过、可追溯用途输入。"""

    dataset_id: UUID
    usage_check_id: UUID
    kind: UsageKind
    input_digest: str
    input_snapshot: Mapping[str, Any]
    base_commit: str
    contract_version: str
    candidate_id: UUID | None


@dataclass(frozen=True, slots=True)
class PublishedDatasetArtifact:
    """供后续训练模块消费的已发布制品及其不可变清单。"""

    dataset_id: UUID
    artifact_id: UUID
    usage_check_id: UUID
    kind: UsageKind
    input_digest: str
    object_key: str
    artifact_sha256: str
    artifact_size: int
    manifest: Mapping[str, Any]


class DatasetCheckedInputReader(Protocol):
    """#33 读取已检查输入和已发布制品的 dataset Interface。"""

    def checked_input(
        self, *, dataset_id: UUID, usage_check_id: UUID
    ) -> CheckedDatasetInput | None:
        """只返回属于数据集且状态为 passed 的冻结输入。"""
        ...

    def published_artifact(
        self, *, dataset_id: UUID, artifact_id: UUID
    ) -> PublishedDatasetArtifact | None:
        """只返回属于数据集且状态为 available 的制品。"""
        ...


class _DatasetCheckedInputReader:
    """在 dataset 内把私有仓储事实转换为 #33 的窄 Interface。"""

    def __init__(self, datasets: UsageDatasetRepository) -> None:
        self._datasets = datasets

    def checked_input(
        self, *, dataset_id: UUID, usage_check_id: UUID
    ) -> CheckedDatasetInput | None:
        check = self._datasets.usage_check_by_id(usage_check_id)
        if (
            check is None
            or check.dataset_id != dataset_id
            or check.status is not UsageCheckStatus.PASSED
        ):
            return None
        return CheckedDatasetInput(
            dataset_id=check.dataset_id,
            usage_check_id=check.id,
            kind=check.kind,
            input_digest=check.input_digest,
            input_snapshot=deepcopy(dict(check.input_snapshot)),
            base_commit=check.base_commit,
            contract_version=check.contract_version,
            candidate_id=check.candidate_id,
        )

    def published_artifact(
        self, *, dataset_id: UUID, artifact_id: UUID
    ) -> PublishedDatasetArtifact | None:
        artifact = self._datasets.artifact_by_id(artifact_id)
        if (
            artifact is None
            or artifact.dataset_id != dataset_id
            or artifact.status is not ArtifactStatus.AVAILABLE
            or not isinstance(artifact.object_key, str)
            or not isinstance(artifact.artifact_sha256, str)
            or not isinstance(artifact.artifact_size, int)
        ):
            return None
        return PublishedDatasetArtifact(
            dataset_id=artifact.dataset_id,
            artifact_id=artifact.id,
            usage_check_id=artifact.usage_check_id,
            kind=artifact.kind,
            input_digest=artifact.input_digest,
            object_key=artifact.object_key,
            artifact_sha256=artifact.artifact_sha256,
            artifact_size=artifact.artifact_size,
            manifest=deepcopy(dict(artifact.manifest)),
        )


def checked_input_reader(datasets: UsageDatasetRepository) -> DatasetCheckedInputReader:
    """为 #33 提供不暴露表和仓储的 dataset Interface。"""
    return _DatasetCheckedInputReader(datasets)


class DatasetResourceLookup(Protocol):
    """供 `job` 确认成员和上传尝试归属的最小查询契约。"""

    def resource_exists(self, member_id: UUID, attempt_id: UUID) -> bool:
        """确认成员与尝试存在且尝试属于该成员。"""
        ...

    def dataset_resource_exists(self, dataset_id: UUID, resource_id: UUID) -> bool:
        """确认用途检查或派生制品属于该数据集。"""
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


class DdmAnnotationGenerator(Protocol):
    """供用途制品 worker 调用 NVIDIA DDM 聚合函数的窄适配器。"""

    def generate(self, workspace: Path, output_filename: str) -> bytes:
        """在隔离工作区生成并读取 DDM annotation。"""
        ...


class DdmReader(Protocol):
    """调用 NVIDIA DDM 训练读取器并返回实际两类样本计数。"""

    def sample_counts(
        self,
        *,
        workspace: Path,
        annotation_filename: str,
        parameters: Mapping[str, object],
    ) -> Mapping[str, int]:
        """构造原读取器并实际读取每类至少一个样本。"""
        ...


class VlmReader(Protocol):
    """调用 NVIDIA VLM 训练读取器消费冻结候选记录。"""

    def validate(
        self,
        *,
        workspace: Path,
        annotation_filename: str,
    ) -> None:
        """构造原读取器并消费全部冻结候选记录。"""
        ...


class DatasetUsageRuntime(Protocol):
    """供用途检查和制品 worker 使用的真实资源工厂。"""

    def repository(self, session: object) -> UsageDatasetRepository:
        """为一个短事务创建用途仓储。"""
        ...

    def storage(self) -> ObjectStorage:
        """创建对象存储客户端，调用发生在数据库事务外。"""
        ...

    def ddm_generator(self) -> DdmAnnotationGenerator:
        """创建复用 NVIDIA DDM 聚合函数的适配器。"""
        ...

    def ddm_reader(self) -> DdmReader:
        """创建调用 NVIDIA DDM 训练读取器的适配器。"""
        ...

    def vlm_reader(self) -> VlmReader:
        """创建调用 NVIDIA VLM 训练读取器的适配器。"""
        ...

    def media_probe(self) -> MediaProbe:
        """创建用途检查使用的媒体事实探测器。"""
        ...

    def annotation_volume(self) -> AnnotationDataVolume:
        """创建标注基座成功输出的只读数据卷 seam。"""
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
    "AnnotationDataVolume",
    "AnnotationDataVolumeUnavailableError",
    "AnnotationExecutionTarget",
    "ArtifactTarget",
    "CheckedDatasetInput",
    "DatasetAnnotationRuntime",
    "DatasetCheckedInputReader",
    "DatasetRefusalCode",
    "DatasetRefusedError",
    "DatasetResourceLookup",
    "DatasetUsageRuntime",
    "DatasetValidationRuntime",
    "DdmAnnotationGenerator",
    "DdmReader",
    "MediaProbeUnavailableError",
    "MemberStatus",
    "ObjectStorageUnavailableError",
    "PreparedAnnotationCopy",
    "PublishedDatasetArtifact",
    "UsageCheckStatus",
    "UsageCheckTarget",
    "UsageKind",
    "VlmReader",
    "apply_usage_check_currentness",
    "begin_annotation_context_preparation",
    "begin_annotation_execution",
    "begin_artifact_generation",
    "begin_usage_check",
    "begin_video_validation",
    "checked_input_reader",
    "complete_annotation_context_preparation",
    "complete_annotation_execution",
    "complete_artifact",
    "complete_artifact_candidate_cleanup",
    "complete_usage_check",
    "fail_annotation_context_preparation",
    "fail_annotation_execution",
    "fail_artifact",
    "fail_usage_check",
    "invalidate_artifact_for_job",
    "mark_artifact_cleanup_pending",
    "prepare_annotation_context_copy",
    "prepare_annotation_execution_copy",
    "record_artifact_orphan_candidate",
    "render_ddm_artifact_with_base",
    "run_usage_check",
    "save_annotation_execution_copy",
    "summary",
    "validate_video_upload",
]
