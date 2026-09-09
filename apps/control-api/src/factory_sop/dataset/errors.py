"""`dataset` 的稳定错误与可执行恢复动作。"""

from __future__ import annotations

from enum import StrEnum


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


class DatasetRefusedError(Exception):
    """业务失败已被分类，调用者可把状态写入记录或映射为问题文档。"""

    def __init__(
        self,
        code: DatasetRefusalCode,
        *,
        detail: str | None = None,
        recovery_action: str | None = None,
    ) -> None:
        super().__init__(detail or code.value)
        self.code = code
        self.detail = detail or code.value
        self.recovery_action = recovery_action


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
        case DatasetRefusalCode.STORAGE_UNAVAILABLE | DatasetRefusalCode.MEDIA_PROBE_UNAVAILABLE:
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
        case _:
            raise AssertionError(f"未处理的 dataset 错误码：{code}")
