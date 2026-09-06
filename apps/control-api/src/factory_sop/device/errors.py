"""Why `device` refused, in the vocabulary the HTTP adapter turns into `problem+json`.

§5.15 makes `error_code` a stable SCREAMING_SNAKE enumeration, a different type from a
judgment's `reason_code`. Backend-specific codes are intentionally not part of this phase's
wire contract; the shared backend row exists only to enforce host history and deletion rules.
"""

from __future__ import annotations

from enum import StrEnum
from typing import assert_never


class DeviceRefusalCode(StrEnum):
    """The host-management `error_code` values emitted in phase one."""

    INFERENCE_HOST_NOT_FOUND = "INFERENCE_HOST_NOT_FOUND"
    INFERENCE_HOST_NAME_TAKEN = "INFERENCE_HOST_NAME_TAKEN"
    INFERENCE_HOST_DEACTIVATED = "INFERENCE_HOST_DEACTIVATED"
    INFERENCE_HOST_HAS_BACKENDS = "INFERENCE_HOST_HAS_BACKENDS"
    STALE_REVISION = "STALE_REVISION"


class DeviceRefusedError(Exception):
    """`device` declined the operation. Carries the code the response reports."""

    def __init__(self, code: DeviceRefusalCode) -> None:
        super().__init__(code.value)
        self.code = code


def refusal_problem(code: DeviceRefusalCode) -> tuple[int, str]:
    """The HTTP status and Simplified Chinese title for a phase-one refusal."""
    match code:
        case DeviceRefusalCode.INFERENCE_HOST_NOT_FOUND:
            return 404, "推理机不存在"
        case DeviceRefusalCode.INFERENCE_HOST_NAME_TAKEN:
            return 409, "已存在同名推理机"
        case DeviceRefusalCode.INFERENCE_HOST_DEACTIVATED:
            return 409, "推理机已停用，恢复后才能继续"
        case DeviceRefusalCode.INFERENCE_HOST_HAS_BACKENDS:
            return 409, "该推理机仍承载推理后端，请先删除它们"
        case DeviceRefusalCode.STALE_REVISION:
            return 409, "内容已被他人修改，请刷新后重试"
        case _:
            assert_never(code)
