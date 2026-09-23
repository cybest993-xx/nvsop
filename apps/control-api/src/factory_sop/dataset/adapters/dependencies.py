"""`dataset` HTTP 与 worker runtime/executor 的依赖装配；组合根可替换这些 seam。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, BinaryIO, cast
from uuid import UUID

from fastapi import Depends, Request
from sqlalchemy.orm import Session as DatabaseSession
from sqlalchemy.orm import sessionmaker

from factory_sop.dataset.adapters.annotation import HttpAnnotationBackend
from factory_sop.dataset.adapters.annotation_volume import LocalAnnotationDataVolume
from factory_sop.dataset.adapters.artifact_execution import PostgresDatasetArtifactExecutor
from factory_sop.dataset.adapters.ddm import NvidiaDdmAnnotationGenerator, NvidiaDdmReader
from factory_sop.dataset.adapters.media import FfprobeMediaProbe
from factory_sop.dataset.adapters.repository import PostgresDatasetRepository
from factory_sop.dataset.adapters.storage import MinioObjectStorage
from factory_sop.dataset.adapters.vlm import NvidiaVlmReader
from factory_sop.dataset.annotation import AnnotationBackend
from factory_sop.dataset.api import (
    DatasetAnnotationRuntime,
    DatasetArtifactExecutor,
    DatasetResourceLookup,
    DatasetUsageRuntime,
    DatasetValidationRuntime,
)
from factory_sop.dataset.media import MediaProbe
from factory_sop.dataset.model import ObjectStat, UploadInstructions
from factory_sop.dataset.repository import DatasetRepository, UsageDatasetRepository
from factory_sop.dataset.storage import ObjectStorage
from factory_sop.job.api import AnnotationJobQueue, UsageJobQueue, ValidationJobQueue
from factory_sop.persistence import RequestSession
from factory_sop.settings import Settings


def datasets(session: RequestSession) -> DatasetRepository:
    """请求事务中的训练数据集、上传和标注记录。"""
    return PostgresDatasetRepository(session)


class _DatasetResourceLookup:
    """用数据集仓储确认任务资源仍然有效。"""

    def __init__(self, datasets: UsageDatasetRepository) -> None:
        self._datasets = datasets

    def resource_exists(self, member_id: UUID, attempt_id: UUID) -> bool:
        """确认成员与上传尝试或标注执行代次归属一致。"""
        member = self._datasets.member_by_id(member_id)
        attempt = self._datasets.attempt_by_id(attempt_id)
        if member is not None and attempt is not None:
            return attempt.member_id == member_id
        return self._datasets.annotation_context_preparation_exists(
            member_id, attempt_id
        ) or self._datasets.annotation_execution_exists(member_id, attempt_id)

    def dataset_resource_exists(self, dataset_id: UUID, resource_id: UUID) -> bool:
        """确认用途检查或制品属于数据集。"""
        for value in (
            self._datasets.usage_check_by_id(resource_id),
            self._datasets.artifact_by_id(resource_id),
        ):
            if value is not None and value.dataset_id == dataset_id:
                return True
        return False


def dataset_resource(
    datasets: Annotated[UsageDatasetRepository, Depends(datasets)],
) -> DatasetResourceLookup:
    """提供 job 查询任务资源归属所需的最小数据集契约。"""
    return _DatasetResourceLookup(datasets)


class _DeferredObjectStorage:
    """延迟创建 MinIO 客户端，避免未授权请求先触碰存储配置或网络。"""

    def __init__(self, factory: Callable[[], ObjectStorage]) -> None:
        self._factory = factory
        self._value: ObjectStorage | None = None

    def _storage(self) -> ObjectStorage:
        if self._value is None:
            self._value = self._factory()
        return self._value

    def create_upload(
        self,
        *,
        object_key: str,
        declared_size: int,
        max_bytes: int,
        expires_at: datetime,
    ) -> UploadInstructions:
        return self._storage().create_upload(
            object_key=object_key,
            declared_size=declared_size,
            max_bytes=max_bytes,
            expires_at=expires_at,
        )

    def stat(self, *, object_key: str) -> ObjectStat:
        return self._storage().stat(object_key=object_key)

    def download_to(
        self,
        *,
        object_key: str,
        destination: BinaryIO,
        version_id: str | None = None,
    ) -> None:
        self._storage().download_to(
            object_key=object_key,
            destination=destination,
            version_id=version_id,
        )

    def finalize_upload(
        self,
        *,
        object_key: str,
        source: BinaryIO,
        size: int,
    ) -> ObjectStat:
        return self._storage().finalize_upload(object_key=object_key, source=source, size=size)

    def delete(self, *, object_key: str) -> None:
        self._storage().delete(object_key=object_key)


def storage(request: Request) -> ObjectStorage:
    """配置的 MinIO 对象存储；实际 SDK 连接按需创建且不提供本地回退。"""
    settings = request.app.state.settings
    return _DeferredObjectStorage(lambda: MinioObjectStorage.from_settings(settings))


def media_probe(request: Request) -> MediaProbe:
    """受配置限制的本地 ffprobe。"""
    settings = request.app.state.settings
    return FfprobeMediaProbe(
        binary=settings.media_probe_binary,
        timeout_seconds=settings.media_probe_timeout_seconds,
    )


def jobs() -> ValidationJobQueue:
    """由组合根接入 job 模块的校验任务创建 seam。"""
    raise RuntimeError("dataset job dependency was not wired")


def annotation_jobs() -> AnnotationJobQueue:
    """由组合根接入 job 模块的标注任务创建 seam。"""
    raise RuntimeError("dataset annotation job dependency was not wired")


def usage_jobs() -> UsageJobQueue:
    """由组合根接入 job 模块的用途任务创建 seam。"""
    raise RuntimeError("dataset usage job dependency was not wired")


class _PostgresDatasetUsageRuntime:
    """为用途 worker 创建真实 PostgreSQL 和 MinIO 资源。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def repository(self, session: object) -> UsageDatasetRepository:
        """把 worker 短事务绑定到真实用途仓储。"""
        return PostgresDatasetRepository(cast(DatabaseSession, session))

    def storage(self) -> ObjectStorage:
        """创建真实 MinIO 对象存储客户端。"""
        return MinioObjectStorage.from_settings(self._settings)

    def ddm_reader(self) -> NvidiaDdmReader:
        """创建调用 NVIDIA DDM 训练读取器的适配器。"""
        return NvidiaDdmReader()

    def vlm_reader(self) -> NvidiaVlmReader:
        """创建调用 NVIDIA VLM 训练读取器的适配器。"""
        return NvidiaVlmReader()

    def media_probe(self) -> MediaProbe:
        """创建用途检查使用的真实 ffprobe 探测器。"""
        return FfprobeMediaProbe(
            binary=self._settings.media_probe_binary,
            timeout_seconds=self._settings.media_probe_timeout_seconds,
        )

    def annotation_volume(self) -> LocalAnnotationDataVolume:
        """创建标注基座成功输出的只读数据卷。"""
        if self._settings.annotation_data_root is None:
            raise RuntimeError("标注基座只读数据卷尚未配置")
        return LocalAnnotationDataVolume(Path(self._settings.annotation_data_root))


class _PostgresDatasetValidationRuntime:
    """为 worker 创建真实 PostgreSQL、MinIO 和 ffprobe 资源。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def repository(self, session: object) -> DatasetRepository:
        """把 worker 短事务绑定到真实数据集仓储。"""
        return PostgresDatasetRepository(cast(DatabaseSession, session))

    def storage(self) -> ObjectStorage:
        """创建真实 MinIO 对象存储客户端。"""
        return MinioObjectStorage.from_settings(self._settings)

    def media_probe(self) -> MediaProbe:
        """创建真实 ffprobe 媒体探测器。"""
        return FfprobeMediaProbe(
            binary=self._settings.media_probe_binary,
            timeout_seconds=self._settings.media_probe_timeout_seconds,
        )

    def supported_codecs(self) -> frozenset[str]:
        """返回配置中的媒体编码集合。"""
        return frozenset(
            item.strip().casefold()
            for item in self._settings.dataset_supported_codecs.split(",")
            if item.strip()
        )


class _PostgresDatasetAnnotationRuntime:
    """为标注 worker 创建真实 PostgreSQL、MinIO 和基座 HTTP 资源。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def repository(self, session: object) -> DatasetRepository:
        """把 worker 短事务绑定到真实数据集仓储。"""
        return PostgresDatasetRepository(cast(DatabaseSession, session))

    def storage(self) -> ObjectStorage:
        """创建真实 MinIO 对象存储客户端。"""
        return MinioObjectStorage.from_settings(self._settings)

    def backend(self) -> AnnotationBackend:
        """创建复用 NVIDIA 标注基座的 HTTP adapter。"""
        return HttpAnnotationBackend.from_settings(self._settings)

    def media_probe(self) -> MediaProbe:
        """创建转码后副本的媒体事实探测器。"""
        return FfprobeMediaProbe(
            binary=self._settings.media_probe_binary,
            timeout_seconds=self._settings.media_probe_timeout_seconds,
        )


def annotation_runtime(settings: Settings) -> DatasetAnnotationRuntime:
    """构造标注 worker 使用的真实运行时。"""
    return _PostgresDatasetAnnotationRuntime(settings)


def artifact_executor(
    settings: Settings,
    factory: sessionmaker[DatabaseSession],
) -> DatasetArtifactExecutor:
    """构造 dataset owner 的制品执行 seam。"""
    return PostgresDatasetArtifactExecutor(
        factory=factory,
        storage_factory=lambda: MinioObjectStorage.from_settings(settings),
        generate=lambda workspace, output_filename: NvidiaDdmAnnotationGenerator().generate(
            workspace,
            output_filename,
        ),
    )


def usage_runtime(settings: Settings) -> DatasetUsageRuntime:
    """构造用途 worker 使用的真实运行时。"""
    return _PostgresDatasetUsageRuntime(settings)


def validation_runtime(settings: Settings) -> DatasetValidationRuntime:
    """构造 worker 使用的真实数据集运行时。"""
    return _PostgresDatasetValidationRuntime(settings)
