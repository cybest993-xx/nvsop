"""`dataset` 的稳定错误与可执行恢复动作。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never


class DatasetRefusalCode(StrEnum):
    """控制面输入、资源归属和状态冲突的错误码。"""

    DATASET_NOT_FOUND = "DATASET_NOT_FOUND"
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    ATTEMPT_NOT_FOUND = "ATTEMPT_NOT_FOUND"
    DATASET_NAME_INVALID = "DATASET_NAME_INVALID"
    FILENAME_INVALID = "FILENAME_INVALID"
    SOURCE_INVALID = "SOURCE_INVALID"
    SIZE_INVALID = "SIZE_INVALID"
    SIZE_EXCEEDED = "SIZE_EXCEEDED"
    SHA256_INVALID = "SHA256_INVALID"
    ARCHIVE_REJECTED = "ARCHIVE_REJECTED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RESOURCE_MISMATCH = "RESOURCE_MISMATCH"
    STATE_CONFLICT = "STATE_CONFLICT"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"
    EMPTY_OBJECT = "EMPTY_OBJECT"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    SHA256_MISMATCH = "SHA256_MISMATCH"
    ARCHIVE_CONTENT_REJECTED = "ARCHIVE_CONTENT_REJECTED"
    INVALID_MEDIA = "INVALID_MEDIA"
    UNSUPPORTED_CODEC = "UNSUPPORTED_CODEC"
    MEDIA_PROBE_UNAVAILABLE = "MEDIA_PROBE_UNAVAILABLE"
    VALIDATION_STALE = "VALIDATION_STALE"
    ACTION_LIST_INVALID = "ACTION_LIST_INVALID"
    ACTION_LIST_NOT_FOUND = "ACTION_LIST_NOT_FOUND"
    ANNOTATION_MEMBER_NOT_REGISTERED = "ANNOTATION_MEMBER_NOT_REGISTERED"
    ANNOTATION_CONTEXT_INVALID = "ANNOTATION_CONTEXT_INVALID"
    ANNOTATION_NOT_FOUND = "ANNOTATION_NOT_FOUND"
    ANNOTATION_IDEMPOTENCY_CONFLICT = "ANNOTATION_IDEMPOTENCY_CONFLICT"
    ANNOTATION_STATE_CONFLICT = "ANNOTATION_STATE_CONFLICT"
    STALE_REVISION = "STALE_REVISION"
    ANNOTATION_BACKEND_UNAVAILABLE = "ANNOTATION_BACKEND_UNAVAILABLE"
    ANNOTATION_EXECUTION_FAILED = "ANNOTATION_EXECUTION_FAILED"
    ANNOTATION_OPERATION_NOT_ALLOWED = "ANNOTATION_OPERATION_NOT_ALLOWED"
    VLM_CANDIDATE_NOT_FOUND = "VLM_CANDIDATE_NOT_FOUND"
    VLM_CANDIDATE_INVALID = "VLM_CANDIDATE_INVALID"
    USAGE_CHECK_NOT_FOUND = "USAGE_CHECK_NOT_FOUND"
    USAGE_STATE_CONFLICT = "USAGE_STATE_CONFLICT"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
    ARTIFACT_UNAVAILABLE = "ARTIFACT_UNAVAILABLE"
    ARTIFACT_INTEGRITY_FAILURE = "ARTIFACT_INTEGRITY_FAILURE"


@dataclass(frozen=True, slots=True)
class DatasetFieldError:
    """数据集拒绝涉及的一个输入字段。"""

    field: str
    message: str


class DatasetRefusedError(Exception):
    """业务失败已被分类，调用者可把状态写入记录或映射为问题文档。"""

    def __init__(
        self,
        code: DatasetRefusalCode,
        *,
        detail: str | None = None,
        recovery_action: str | None = None,
        field_errors: tuple[DatasetFieldError, ...] = (),
    ) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail or code.value
        self.recovery_action = recovery_action
        self.field_errors = field_errors


def refusal_problem(code: DatasetRefusalCode) -> tuple[int, str]:
    """把 dataset 错误映射为稳定 HTTP 状态和简体中文标题。"""
    match code:
        case DatasetRefusalCode.DATASET_NOT_FOUND | DatasetRefusalCode.MEMBER_NOT_FOUND:
            return 404, "训练数据集或视频不存在"
        case DatasetRefusalCode.ATTEMPT_NOT_FOUND:
            return 404, "上传尝试不存在"
        case DatasetRefusalCode.DATASET_NAME_INVALID | DatasetRefusalCode.FILENAME_INVALID:
            return 422, "训练数据集输入不符合要求"
        case DatasetRefusalCode.SOURCE_INVALID:
            return 422, "视频来源不符合要求"
        case (
            DatasetRefusalCode.SIZE_INVALID
            | DatasetRefusalCode.SIZE_EXCEEDED
            | DatasetRefusalCode.SHA256_INVALID
            | DatasetRefusalCode.ARCHIVE_REJECTED
        ):
            return 422, "视频上传声明不符合要求"
        case DatasetRefusalCode.IDEMPOTENCY_CONFLICT | DatasetRefusalCode.STATE_CONFLICT:
            return 409, "视频当前状态不允许该操作"
        case DatasetRefusalCode.RESOURCE_MISMATCH:
            return 404, "请求的资源归属不匹配"
        case (
            DatasetRefusalCode.STORAGE_UNAVAILABLE
            | DatasetRefusalCode.MEDIA_PROBE_UNAVAILABLE
            | DatasetRefusalCode.ARTIFACT_INTEGRITY_FAILURE
        ):
            return 503, "基础设施暂时不可用"
        case (
            DatasetRefusalCode.OBJECT_NOT_FOUND
            | DatasetRefusalCode.EMPTY_OBJECT
            | DatasetRefusalCode.SIZE_MISMATCH
            | DatasetRefusalCode.SHA256_MISMATCH
            | DatasetRefusalCode.ARCHIVE_CONTENT_REJECTED
            | DatasetRefusalCode.INVALID_MEDIA
            | DatasetRefusalCode.UNSUPPORTED_CODEC
        ):
            return 422, "视频校验失败"
        case DatasetRefusalCode.VALIDATION_STALE:
            return 409, "上传尝试已不是当前尝试"
        case DatasetRefusalCode.ACTION_LIST_INVALID:
            return 422, "动作清单不符合要求"
        case DatasetRefusalCode.ACTION_LIST_NOT_FOUND:
            return 404, "动作清单修订不存在"
        case DatasetRefusalCode.ANNOTATION_MEMBER_NOT_REGISTERED:
            return 409, "视频尚未完成校验，不能标注"
        case DatasetRefusalCode.ANNOTATION_CONTEXT_INVALID:
            return 404, "标注上下文无效或已过期"
        case DatasetRefusalCode.ANNOTATION_NOT_FOUND:
            return 404, "标注提交不存在"
        case DatasetRefusalCode.ANNOTATION_IDEMPOTENCY_CONFLICT:
            return 409, "标注幂等键对应的内容不同"
        case DatasetRefusalCode.ANNOTATION_STATE_CONFLICT:
            return 409, "标注当前状态不允许该操作"
        case DatasetRefusalCode.STALE_REVISION:
            return 409, "标注修订号已变化（STALE_REVISION）"
        case DatasetRefusalCode.ANNOTATION_BACKEND_UNAVAILABLE:
            return 503, "标注服务暂时不可用"
        case DatasetRefusalCode.ANNOTATION_EXECUTION_FAILED:
            return 422, "标注切片执行失败"
        case DatasetRefusalCode.ANNOTATION_OPERATION_NOT_ALLOWED:
            return 403, "标注操作未开放"
        case (
            DatasetRefusalCode.VLM_CANDIDATE_NOT_FOUND
            | DatasetRefusalCode.USAGE_CHECK_NOT_FOUND
            | DatasetRefusalCode.ARTIFACT_NOT_FOUND
        ):
            return 404, "训练数据用途记录不存在"
        case DatasetRefusalCode.VLM_CANDIDATE_INVALID:
            return 422, "VLM 候选输入不符合要求"
        case DatasetRefusalCode.USAGE_STATE_CONFLICT | DatasetRefusalCode.ARTIFACT_UNAVAILABLE:
            return 409, "训练数据用途当前状态不允许该操作"
        case _:
            assert_never(code)
