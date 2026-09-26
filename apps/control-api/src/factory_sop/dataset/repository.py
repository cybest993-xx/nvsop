"""`dataset` 用例访问 PostgreSQL 的唯一仓储 seam，涵盖上传与标注生命周期。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from factory_sop.dataset.model import (
    ActionListRevision,
    AnnotationContext,
    AnnotationExecution,
    AnnotationSubmission,
    DatasetArtifact,
    DatasetMember,
    TrainingDataset,
    UploadAttempt,
    UsageCheck,
    UsageKind,
    VlmCandidate,
)


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

    def lock_annotation_member(self, member_id: UUID) -> DatasetMember | None:
        """锁定并重新读取视频成员行，串行化标注修订分配。"""
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

    def expired_pending_attempts(self, *, now: datetime, limit: int) -> Sequence[UploadAttempt]:
        """读取已过期且仍未上传完的尝试，供本地媒体回收。"""
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

    def add_action_list(self, value: ActionListRevision) -> None:
        """追加一份动作清单修订；历史修订不修改。"""
        ...

    def latest_action_list(self, dataset_id: UUID) -> ActionListRevision | None:
        """读取数据集最新动作清单修订。"""
        ...

    def action_list_by_revision(
        self, *, dataset_id: UUID, revision: int
    ) -> ActionListRevision | None:
        """读取数据集指定动作清单修订。"""
        ...

    def add_annotation_context(self, value: AnnotationContext) -> None:
        """保存一个短期标注上下文。"""
        ...

    def annotation_context_by_id(self, context_id: UUID) -> AnnotationContext | None:
        """按内部上下文身份读取标注上下文。"""
        ...

    def save_annotation_context(self, value: AnnotationContext) -> None:
        """保存上下文准备好的基座资源身份；不提交事务。"""
        ...

    def annotation_submission_by_idempotency(
        self, *, dataset_id: UUID, idempotency_key: str
    ) -> AnnotationSubmission | None:
        """按数据集范围的幂等键读取标注业务提交。"""
        ...

    def annotation_submission_by_id(self, submission_id: UUID) -> AnnotationSubmission | None:
        """按业务提交身份读取标注提交。"""
        ...

    def list_annotation_submissions(
        self, *, dataset_id: UUID, member_id: UUID
    ) -> Sequence[AnnotationSubmission]:
        """按时间顺序读取视频的全部标注业务提交。"""
        ...

    def add_annotation_submission(self, value: AnnotationSubmission) -> None:
        """追加标注业务提交；不提交事务。"""
        ...

    def annotation_execution_by_id(self, execution_id: UUID) -> AnnotationExecution | None:
        """按执行代次身份读取标注候选。"""
        ...

    def latest_annotation_execution(self, submission_id: UUID) -> AnnotationExecution | None:
        """读取某次业务提交的最新执行代次。"""
        ...

    def list_annotation_executions(self, submission_id: UUID) -> Sequence[AnnotationExecution]:
        """按代次顺序读取一项提交的所有候选。"""
        ...

    def add_annotation_execution(self, value: AnnotationExecution) -> None:
        """追加标注候选执行代次；不提交事务。"""
        ...

    def save_annotation_execution(
        self, value: AnnotationExecution, *, expected_updated_at: datetime
    ) -> bool:
        """按更新时间租约保存执行状态，防止过期 worker 覆盖新代次。"""
        ...

    def annotation_context_preparation_exists(self, member_id: UUID, context_id: UUID) -> bool:
        """确认标注上下文属于视频成员。"""
        ...

    def annotation_execution_exists(self, member_id: UUID, execution_id: UUID) -> bool:
        """确认标注执行代次属于视频成员。"""
        ...


class UsageDatasetRepository(DatasetRepository, Protocol):
    """用途检查额外依赖的仓储方法，不改变既有上传/标注测试 seam。"""

    def list_members(self, *, dataset_id: UUID) -> Sequence[DatasetMember]:
        """返回用途检查需要冻结的全部视频成员。"""
        ...

    def latest_annotation_submission(self, *, member_id: UUID) -> AnnotationSubmission | None:
        """读取某视频最新一份已保存标注。"""
        ...

    def add_vlm_candidate(self, value: VlmCandidate) -> None:
        """追加一份不可变 VLM 候选修订。"""
        ...

    def vlm_candidate_by_id(self, candidate_id: UUID) -> VlmCandidate | None:
        """按身份读取 VLM 候选。"""
        ...

    def latest_vlm_candidate(self, dataset_id: UUID) -> VlmCandidate | None:
        """读取数据集当前 VLM 候选修订。"""
        ...

    def list_vlm_candidates(self, dataset_id: UUID) -> Sequence[VlmCandidate]:
        """读取 VLM 候选历史。"""
        ...

    def add_usage_check(self, value: UsageCheck) -> None:
        """追加一条用途检查。"""
        ...

    def usage_check_by_id(self, check_id: UUID) -> UsageCheck | None:
        """按身份读取用途检查。"""
        ...

    def list_usage_checks(self, dataset_id: UUID) -> Sequence[UsageCheck]:
        """读取用途检查历史。"""
        ...

    def latest_usage_check(self, *, dataset_id: UUID, kind: UsageKind) -> UsageCheck | None:
        """读取一种用途最近一次检查。"""
        ...

    def save_usage_check(self, value: UsageCheck, *, expected_updated_at: datetime) -> bool:
        """按执行租约写回用途检查。"""
        ...

    def add_artifact(self, value: DatasetArtifact) -> None:
        """追加制品记录。"""
        ...

    def artifact_by_id(self, artifact_id: UUID) -> DatasetArtifact | None:
        """按身份读取制品。"""
        ...

    def artifact_by_input(self, *, dataset_id: UUID, input_digest: str) -> DatasetArtifact | None:
        """按数据集和冻结输入摘要复用已成功制品。"""
        ...

    def list_artifacts(self, dataset_id: UUID) -> Sequence[DatasetArtifact]:
        """读取数据集制品历史。"""
        ...

    def list_artifact_cleanup_candidates(self, *, limit: int) -> Sequence[DatasetArtifact]:
        """读取持久化记录了未发布候选键、等待对象清理的制品。"""
        ...

    def save_artifact(self, value: DatasetArtifact, *, expected_updated_at: datetime) -> bool:
        """按执行租约写回制品。"""
        ...
