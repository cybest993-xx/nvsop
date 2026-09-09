"""`dataset` 用例访问 PostgreSQL 的唯一仓储 seam。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.dataset.model import DatasetMember, TrainingDataset, UploadAttempt


class DatasetRepository(Protocol):
    """训练数据集、成员和上传尝试的组合仓储；任何方法都不提交事务。"""

    def add_dataset(self, dataset: TrainingDataset) -> None:
        """保存训练数据集。"""
        ...

    def dataset_by_id(self, dataset_id: UUID) -> TrainingDataset | None:
        """按公开身份读取训练数据集。"""
        ...

    def page_datasets(self, *, page: int, page_size: int) -> tuple[Sequence[TrainingDataset], int]:
        """返回一页训练数据集和总数。"""
        ...

    def add_member(self, member: DatasetMember) -> None:
        """保存一个视频成员。"""
        ...

    def member_by_id(self, member_id: UUID) -> DatasetMember | None:
        """按公开身份读取视频成员。"""
        ...

    def page_members(
        self, *, dataset_id: UUID, page: int, page_size: int
    ) -> tuple[Sequence[DatasetMember], int]:
        """返回指定训练数据集的一页视频成员和总数。"""
        ...

    def add_attempt(self, attempt: UploadAttempt) -> None:
        """保存独立上传尝试。"""
        ...

    def attempt_by_id(self, attempt_id: UUID) -> UploadAttempt | None:
        """按公开身份读取上传尝试。"""
        ...

    def attempt_by_idempotency(
        self, *, dataset_id: UUID, idempotency_key: str
    ) -> UploadAttempt | None:
        """按数据集范围的幂等键读取已有尝试。"""
        ...

    def save_member(
        self,
        member: DatasetMember,
        *,
        expected_attempt_id: UUID,
        expected_updated_at: datetime | None = None,
    ) -> bool:
        """按当前尝试和可选租约更新时间更新成员，返回是否仍可写回。"""
        ...

    def save_attempt(self, attempt: UploadAttempt) -> None:
        """更新上传尝试状态；不提交事务。"""
        ...
