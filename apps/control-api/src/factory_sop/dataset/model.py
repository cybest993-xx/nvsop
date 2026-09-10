"""训练数据集、视频成员和独立上传尝试的纯领域类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
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
    declared_sha256: str
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
    declared_sha256: str
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
    """服务端从对象存储读取的事实，而非客户端声明。"""

    size: int
    version_id: str | None


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
