"""训练数据集、视频成员、动作标注和独立上传尝试的纯领域类型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from factory_sop.job.api import ApplicationJob


class MemberStatus(StrEnum):
    """视频成员可观察的接收与校验状态。"""

    PENDING_UPLOAD = "pending_upload"
    PENDING_VALIDATION = "pending_validation"
    VALIDATING = "validating"
    REGISTERED = "registered"
    FAILED = "failed"


class AttemptStatus(StrEnum):
    """一次上传尝试的状态；旧尝试不能再写回当前成员。"""

    PENDING_UPLOAD = "pending_upload"
    PENDING_VALIDATION = "pending_validation"
    REGISTERED = "registered"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class RetryMode(StrEnum):
    """失败后的恢复动作，直接表达调用者下一步该做什么。"""

    UPLOAD = "retry_upload"
    VALIDATION = "retry_validation"


class AnnotationMode(StrEnum):
    """标注界面允许的时间段关系。"""

    SINGLE_OPERATOR = "single_operator"
    TWO_OPERATOR = "two_operator"


class AnnotationExecutionStatus(StrEnum):
    """一次切片候选的执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AnnotationPreparationStatus(StrEnum):
    """标注上下文基座副本的准备状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class VlmCandidateKind(StrEnum):
    """基座支持的 VLM 输入产物类别。"""

    GQA = "gqa"
    BCQ = "bcq"
    MCQ = "mcq"
    GOLDEN_GQA = "golden_gqa"


class UsageKind(StrEnum):
    """训练数据用途。"""

    DDM = "ddm"
    VLM = "vlm"


class UsageCheckStatus(StrEnum):
    """用途检查的持久化状态。"""

    UNCHECKED = "unchecked"
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class ArtifactStatus(StrEnum):
    """派生制品的持久化状态。"""

    NOT_GENERATED = "not_generated"
    PENDING = "pending"
    RUNNING = "running"
    AVAILABLE = "available"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DdmVideoInput:
    """一次 DDM 检查冻结的一段完整源视频和动作时间段。"""

    member_id: UUID
    object_version_id: str
    source_sha256: str
    duration_seconds: float
    action_list_revision: int
    annotation_revision: int
    mode: AnnotationMode
    actions: tuple[str, ...]
    segments: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class VlmMediaReference:
    """VLM 候选中由服务端绑定的媒体身份。"""

    key: str
    member_id: UUID
    source_object_version_id: str
    source_sha256: str
    annotation_submission_id: UUID | None = None
    annotation_execution_id: UUID | None = None
    clip_index: int | None = None
    action_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class VlmCandidate:
    """一份不可变的 VLM 输入修订。"""

    id: UUID
    dataset_id: UUID
    revision: int
    kind: VlmCandidateKind
    action_list_revision: int
    records: tuple[Mapping[str, Any], ...]
    media: tuple[VlmMediaReference, ...]
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UsageCheck:
    """一次冻结输入的用途检查及其可追溯结果。"""

    id: UUID
    dataset_id: UUID
    kind: UsageKind
    status: UsageCheckStatus
    input_digest: str
    input_snapshot: Mapping[str, Any]
    summary: Mapping[str, int]
    issues: tuple[Mapping[str, Any], ...]
    base_commit: str
    contract_version: str
    candidate_id: UUID | None
    job_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DatasetArtifact:
    """不可覆盖的 DDM annotation 制品及其清单摘要。"""

    id: UUID
    dataset_id: UUID
    usage_check_id: UUID
    kind: UsageKind
    status: ArtifactStatus
    input_digest: str
    object_key: str | None
    artifact_sha256: str | None
    artifact_size: int | None
    manifest: Mapping[str, Any]
    failure_code: str | None
    failure_detail: str | None
    job_id: UUID | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime
    retryable: bool = False
    recovery_action: str | None = None


@dataclass(frozen=True, slots=True)
class ActionListRevision:
    """训练数据集的一份不可变动作清单修订。"""

    dataset_id: UUID
    revision: int
    actions: tuple[str, ...]
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AnnotationContext:
    """绑定数据集、视频、动作清单和源对象代次的短期标注上下文。"""

    id: UUID
    dataset_id: UUID
    member_id: UUID
    action_list_revision: int
    annotation_revision: int
    source_object_version_id: str
    source_sha256: str
    created_by: UUID
    created_at: datetime
    expires_at: datetime
    token: str = ""
    upstream_data_id: str | None = None
    upstream_video_id: str | None = None
    upstream_video_size: int | None = None
    upstream_video_sha256: str | None = None
    upstream_video_duration_seconds: float | None = None
    preparation_job_id: UUID | None = None
    preparation_status: str = AnnotationPreparationStatus.PENDING.value
    preparation_failure_code: str | None = None
    preparation_failure_detail: str | None = None


@dataclass(frozen=True, slots=True)
class AnnotationSegment:
    """一个动作时间段；动作编号沿用基座的零起始索引。"""

    start: float
    end: float
    action_index: int
    action_description: str

    def as_wire(self) -> dict[str, Any]:
        """转换为基座 split endpoint 使用的字段。"""
        return {
            "start": self.start,
            "end": self.end,
            "actionIndex": self.action_index,
            "actionDescription": self.action_description,
        }


@dataclass(frozen=True, slots=True)
class AnnotationSubmission:
    """一次稳定的标注业务提交；重试不改变它的身份。"""

    id: UUID
    dataset_id: UUID
    member_id: UUID
    context_id: UUID
    revision: int
    action_list_revision: int
    source_object_version_id: str
    source_sha256: str
    idempotency_key: str
    request_digest: str
    mode: AnnotationMode
    segments: tuple[AnnotationSegment, ...]
    raw_segments: tuple[dict[str, Any], ...]
    created_by: UUID
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AnnotationExecution:
    """一次可重试的切片候选代次；完成后的候选内容只追加不覆盖。"""

    id: UUID
    submission_id: UUID
    generation: int
    job_id: UUID | None
    status: AnnotationExecutionStatus
    clips: tuple[dict[str, Any], ...]
    failure_code: str | None
    failure_detail: str | None
    created_at: datetime
    updated_at: datetime
    upstream_data_id: str | None = None
    upstream_video_id: str | None = None
    derived_video_size: int | None = None
    derived_video_sha256: str | None = None
    derived_video_duration_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class TrainingDataset:
    """一个有名称的训练数据集分组。"""

    id: UUID
    name: str
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DatasetMember:
    """数据集内一个独立视频及其声明、验证事实和当前尝试。"""

    id: UUID
    dataset_id: UUID
    original_filename: str
    source: str
    declared_size: int
    declared_sha256: str | None
    current_attempt_id: UUID
    status: str
    actual_size: int | None
    actual_sha256: str | None
    duration_seconds: float | None
    codec: str | None
    container: str | None
    object_key: str | None
    object_version_id: str | None
    validation_job_id: UUID | None
    failure_code: str | None
    failure_detail: str | None
    recovery_action: str | None
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UploadAttempt:
    """一次不可复用的对象写入目标和声明快照。"""

    id: UUID
    dataset_id: UUID
    member_id: UUID
    idempotency_key: str | None
    object_key: str
    declared_size: int
    declared_sha256: str | None
    expires_at: datetime
    status: str
    created_at: datetime
    validation_job_id: UUID | None
    object_version_id: str | None


@dataclass(frozen=True, slots=True)
class UploadInstructions:
    """只在申请上传响应中返回的短期对象写说明。"""

    method: str
    url: str
    fields: dict[str, str]
    headers: dict[str, str]
    expires_at: datetime
    max_bytes: int
    object_key: str


@dataclass(frozen=True, slots=True)
class ObjectStat:
    """服务端从媒体存储读取的事实，而非客户端声明。"""

    size: int


@dataclass(frozen=True, slots=True)
class UploadRequestResult:
    """上传申请返回的视频身份、尝试和短期说明。"""

    member: DatasetMember
    attempt: UploadAttempt
    upload: UploadInstructions | None


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """确认上传后的持久状态和可查询任务。"""

    member: DatasetMember
    job: ApplicationJob | None


@dataclass(frozen=True, slots=True)
class RetryResult:
    """失败恢复返回新的上传说明或同一固定内容的校验任务。"""

    member: DatasetMember
    attempt: UploadAttempt
    upload: UploadInstructions | None
    job: ApplicationJob | None
